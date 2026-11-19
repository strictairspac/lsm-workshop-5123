Schema v1: users, audit, datasets (JSON write sequence), configs (bounded LSM parameters), trials (immutable input snapshot and JSON result). MySQL stores metadata only. Run python -m scripts.seed_demo for create-if-missing and natural-key seed; existing values are not overwritten.

Schema v2: 新增 space_plans（批次与 memtable 快照、固定字节口径方案、可续跑进度 state）。原 trials 表结构不变；空间回收不改写试验结果。启动时 create_all 按缺表创建。
