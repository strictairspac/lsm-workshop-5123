import json,unittest
from src.services.space_planner import (
    SpacePlanError,build_plan,flush_tables,initial_state,apply_step,materialize,
    read_values,oracle_values,verify_reads,table_bytes,merge_tables,encode_table,
)
DEMO=[{'op':'put','key':'sensor-01','value':'18.2'},{'op':'put','key':'sensor-02','value':'19.1'},{'op':'put','key':'sensor-03','value':'22.4'},{'op':'put','key':'sensor-04','value':'17.0'},{'op':'put','key':'sensor-01','value':'18.8'},{'op':'delete','key':'sensor-02'},{'op':'put','key':'sensor-05','value':'21.6'},{'op':'put','key':'sensor-06','value':'20.3'},{'op':'delete','key':'sensor-03'},{'op':'put','key':'sensor-07','value':'23.0'},{'op':'put','key':'sensor-01','value':'19.0'},{'op':'put','key':'sensor-08','value':'18.9'}]
ALLDEL=[{'op':'put','key':'x','value':'1'},{'op':'put','key':'y','value':'2'},{'op':'delete','key':'x'},{'op':'delete','key':'y'}]
REVIVE=[{'op':'put','key':'sensor-a','value':'old'},{'op':'put','key':'sensor-b','value':'b'},{'op':'put','key':'sensor-c','value':'c'},{'op':'put','key':'sensor-d','value':'d'},{'op':'delete','key':'sensor-a'},{'op':'put','key':'sensor-e','value':'e'}]
def big(n=24,cap=3):
    return [{'op':'put','key':f'k{i%7}','value':str(i)} if i%9 else {'op':'delete','key':f'k{i%7}'} for i in range(n)],cap
class PlannerTest(unittest.TestCase):
    def test_encoding_is_fixed_and_sorted(self):
        tables=flush_tables(DEMO,2)
        snap=json.loads(encode_table(tables[0]))
        self.assertEqual(list(snap),sorted(snap))
        self.assertEqual(sum(table_bytes(t) for t in tables),351)
    def test_more_than_eight_tables_is_rejected(self):
        entries,cap=big(40,3)
        tables=flush_tables(entries,cap)
        self.assertGreater(len(tables),8)
        with self.assertRaises(SpacePlanError):
            build_plan(tables,10**6,1)
    def test_demo_plan_executes_and_hits_target(self):
        tables=flush_tables(DEMO,2)
        plan=build_plan(tables,411,178)
        self.assertTrue(plan['feasible'],plan['reason'])
        self.assertGreater(len(plan['steps']),1)
        state=initial_state(tables,plan)
        for step in plan['steps']:
            state=apply_step(tables,state,step)
            self.assertLessEqual(state['peak_bytes'],4096)
        self.assertEqual(read_values(materialize(tables,state['groups'],state['dropped'])),oracle_values(DEMO))
        self.assertLessEqual(state['current_bytes'],178)
        self.assertEqual(verify_reads(tables,state,DEMO)['ok'],True)
    def test_tombstones_survive_partial_merges(self):
        tables=flush_tables(DEMO,2)
        plan=build_plan(tables,411,178)
        for step in plan['steps'][:-1]:
            self.assertFalse(step['drop_tombstones'])
            self.assertEqual(step['tombstones_removed'],0)
    def test_all_deleted_reaches_zero_bytes(self):
        tables=flush_tables(ALLDEL,2)
        plan=build_plan(tables,4096,0)
        self.assertTrue(plan['feasible'],plan['reason'])
        state=initial_state(tables,plan)
        for step in plan['steps']:
            state=apply_step(tables,state,step)
        self.assertEqual(state['current_bytes'],0)
        self.assertEqual(read_values(materialize(tables,state['groups'],state['dropped'])),{})
    def test_infeasible_when_target_below_minimum(self):
        tables=flush_tables(DEMO,2)
        plan=build_plan(tables,4096,1)
        self.assertFalse(plan['feasible']);self.assertIn('最少',plan['reason']);self.assertEqual(plan['steps'],[])
    def test_target_must_be_strictly_below_capacity(self):
        tables=flush_tables(DEMO,2)
        with self.assertRaises(SpacePlanError):
            build_plan(tables,400,400)
    def test_infeasible_when_capacity_below_initial(self):
        tables=flush_tables(DEMO,2)
        plan=build_plan(tables,100,10)
        self.assertFalse(plan['feasible']);self.assertIn('容量上限',plan['reason'])
    def test_replay_same_step_is_rejected_by_planner(self):
        tables=flush_tables(DEMO,2)
        plan=build_plan(tables,411,178)
        state=initial_state(tables,plan)
        state=apply_step(tables,state,plan['steps'][0])
        with self.assertRaises(SpacePlanError):
            apply_step(tables,state,plan['steps'][0])
    def test_drop_happens_only_in_a_step_covering_all_tables(self):
        tables=flush_tables(DEMO,2)
        for cap in (411,470,10**6):
            plan=build_plan(tables,cap,178)
            self.assertTrue(plan['feasible'],plan['reason'])
            drops=[s for s in plan['steps'] if s['drop_tombstones']]
            self.assertEqual(len(drops),1)
            self.assertEqual(drops[0]['merged'],sorted(range(len(tables))))
            covered=sorted({i for g in drops[0]['inputs'] for i in g})
            self.assertEqual(covered,sorted(range(len(tables))))
            self.assertIs(plan['steps'][-1],drops[0])
            for step in plan['steps'][:-1]:
                self.assertFalse(step['drop_tombstones'])
    def test_old_value_never_revives_between_steps(self):
        tables=flush_tables(REVIVE,2)
        self.assertGreater(len(tables),1)
        plan=build_plan(tables,10**6,table_bytes(merge_tables(tables,drop_tombstones=True)))
        self.assertTrue(plan['feasible'],plan['reason'])
        state=initial_state(tables,plan)
        self.assertNotIn('sensor-a',state['read_values'])
        for step in plan['steps']:
            state=apply_step(tables,state,step)
            result=verify_reads(tables,state,REVIVE)
            self.assertTrue(result['ok'])
            self.assertNotIn('sensor-a',result['actual'])
    def test_peak_accounts_for_both_copies(self):
        tables=flush_tables(DEMO,2)
        plan=build_plan(tables,411,178)
        initial=sum(table_bytes(t) for t in tables)
        s0=plan['steps'][0]
        self.assertEqual(s0['total_before'],initial)
        self.assertEqual(s0['peak_bytes'],s0['total_before']+s0['output_bytes'])
if __name__=='__main__':unittest.main()
