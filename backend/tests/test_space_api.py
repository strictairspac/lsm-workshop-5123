import json,os,tempfile,unittest
from pathlib import Path

def _deps_available():
    try:
        import fastapi,sqlalchemy,jwt,httpx  # noqa: F401
        return True
    except Exception:
        return False

DEMO=[{'op':'put','key':'sensor-01','value':'18.2'},{'op':'put','key':'sensor-02','value':'19.1'},{'op':'put','key':'sensor-03','value':'22.4'},{'op':'put','key':'sensor-04','value':'17.0'},{'op':'put','key':'sensor-01','value':'18.8'},{'op':'delete','key':'sensor-02'},{'op':'put','key':'sensor-05','value':'21.6'},{'op':'put','key':'sensor-06','value':'20.3'},{'op':'delete','key':'sensor-03'},{'op':'put','key':'sensor-07','value':'23.0'},{'op':'put','key':'sensor-01','value':'19.0'},{'op':'put','key':'sensor-08','value':'18.9'}]

@unittest.skipUnless(_deps_available(),'API dependencies are not installed')
class SpacePlanApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        db_path=Path(cls.tmp.name)/'api.db'
        os.environ['DATABASE_URL']=f'sqlite:///{db_path}'
        os.environ['REDIS_URL']='redis://localhost:6379/0'
        os.environ['JWT_SECRET']='test-secret'
        from src.config.database import Base,engine,Session
        from src.models import audit,config,dataset,space_plan,trial,user  # noqa: F401
        from src.models.user import User
        from src.models.dataset import Dataset
        from src.utils.password import hash_password
        Base.metadata.create_all(engine)
        with Session() as db:
            db.add(User(email='admin@t.test',role='admin',password_hash=hash_password('pw')))
            db.add(User(email='viewer@t.test',role='viewer',password_hash=hash_password('pw')))
            db.add(Dataset(name='demo',description='d',entries=DEMO))
            db.commit()
        from fastapi.testclient import TestClient
        from src.main import app
        cls.client=TestClient(app)
        cls.headers={}
        for role in ('admin','viewer'):
            response=cls.client.post('/auth/login',json={'email':f'{role}@t.test','password':'pw'})
            cls.headers[role]={'Authorization':'Bearer '+response.json()['token']}
    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def create(self,name,capacity=411,target=178):
        name=self._testMethodName+'-'+name
        response=self.client.post('/space-plans',headers=self.headers['admin'],json={
            'name':name,'dataset_id':1,'memtable_limit':2,
            'capacity_bytes':capacity,'target_bytes':target,
        })
        return response

    def test_plan_lifecycle_resume_and_replay(self):
        response=self.create('lifecycle')
        self.assertEqual(response.status_code,201,response.text)
        plan=response.json()
        self.assertTrue(plan['feasible'])
        self.assertEqual(plan['status'],'planned')
        self.assertEqual(plan['initial_bytes'],351)
        self.assertEqual(plan['minimum_bytes'],178)
        ident=plan['id']
        peaks=[]
        for expected in range(plan['total_steps']):
            response=self.client.post(f'/space-plans/{ident}/advance',headers=self.headers['admin'],json={'expected_step':expected})
            self.assertEqual(response.status_code,200,response.text)
            body=response.json()
            self.assertTrue(body['applied'])
            self.assertFalse(body['replayed'])
            self.assertTrue(body['reads_match_oracle'])
            peaks.append(body['peak_bytes'])
            # Retrying the same attempt must not move progress or the peak.
            retry=self.client.post(f'/space-plans/{ident}/advance',headers=self.headers['admin'],json={'expected_step':expected})
            self.assertEqual(retry.status_code,200)
            self.assertFalse(retry.json()['applied'])
            self.assertTrue(retry.json()['replayed'])
            self.assertEqual(retry.json()['step_index'],body['step_index'])
            self.assertEqual(retry.json()['peak_bytes'],body['peak_bytes'])
        final=self.client.get(f'/space-plans/{ident}',headers=self.headers['viewer']).json()
        self.assertEqual(final['status'],'reached')
        self.assertEqual(final['current_bytes'],178)
        self.assertEqual(final['peak_bytes'],max(peaks))
        self.assertLessEqual(final['peak_bytes'],final['capacity_bytes'])
        self.assertEqual(final['actual_checksum'],final['oracle_checksum'])
        self.assertNotIn('sensor-02',final['read_values'])
        self.assertNotIn('sensor-03',final['read_values'])
        self.assertEqual(final['read_values']['sensor-01'],'19.0')

    def test_viewer_cannot_write(self):
        response=self.create('viewer-block')
        ident=response.json()['id']
        denied=self.client.post(f'/space-plans/{ident}/advance',headers=self.headers['viewer'],json={'expected_step':0})
        self.assertEqual(denied.status_code,403)
        denied=self.client.post('/space-plans',headers=self.headers['viewer'],json={
            'name':'nope','dataset_id':1,'memtable_limit':2,'capacity_bytes':411,'target_bytes':178})
        self.assertEqual(denied.status_code,403)

    def test_infeasible_and_validation(self):
        response=self.create('tight',target=1)
        self.assertEqual(response.status_code,201)
        self.assertEqual(response.json()['status'],'infeasible')
        self.assertEqual(response.json()['plan']['steps'],[])
        blocked=self.client.post(f'/space-plans/{response.json()["id"]}/advance',headers=self.headers['admin'],json={'expected_step':0})
        self.assertEqual(blocked.status_code,409)
        self.assertEqual(self.create('target-not-below-cap',411,411).status_code,422)
        self.assertEqual(self.create('dup').status_code,201)
        self.assertEqual(self.create('dup').status_code,409)
        missing=self.client.post('/space-plans',headers=self.headers['admin'],json={
            'name':'missing','dataset_id':999,'memtable_limit':2,'capacity_bytes':411,'target_bytes':178})
        self.assertEqual(missing.status_code,404)
