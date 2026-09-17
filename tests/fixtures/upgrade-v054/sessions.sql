BEGIN TRANSACTION;
CREATE TABLE "_yoyo_log" (
            "id" VARCHAR(36),
            "migration_hash" VARCHAR(64),
            "migration_id" VARCHAR(255),
            "operation" VARCHAR(10),
            "username" VARCHAR(255),
            "hostname" VARCHAR(255),
            "comment" VARCHAR(255),
            "created_at_utc" TIMESTAMP,
            PRIMARY KEY ("id")
        );
INSERT INTO "_yoyo_log" VALUES('2333ef38-5a87-4325-b1fa-fd696bd12add','f8a5255cbc8d7094d88289b6e1dd02bb73fbffbdf97ced47d88799ceb4cfde6e','V001__initial_schema','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.316877');
INSERT INTO "_yoyo_log" VALUES('ac46d3e5-8ee9-468d-a2cf-f0b34c058447','335ac2e449bfe0f30bec8a0308c76b51ec10f480ec88aeef7250ce769e8a70ee','V002__scheduler_session_fields','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.320909');
INSERT INTO "_yoyo_log" VALUES('b2117077-abfe-4ccf-9f90-6004ae34c325','2732174eb02055fd93931e860feaa8b68879cbe1cb984f7d3616d5392f75bb8e','V003__heartbeat_ticks','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.328365');
INSERT INTO "_yoyo_log" VALUES('8aa12f8c-0489-48cd-9518-8f546286882e','3a223b7c95ec8c0bbfd9064e8b766d225788f03f761dd9f5081ef0eef7295f6b','V004__memory_schema_version','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.331372');
INSERT INTO "_yoyo_log" VALUES('773a521e-032b-492f-893f-ee2fd558a0cf','8480378c8c5f6f322c1707a8b01f56d67a8b392ea67b6f8e199b2b36aeb1468f','V005__agent_tasks','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.336384');
INSERT INTO "_yoyo_log" VALUES('01650603-68e9-4cd3-92a5-1294299ce18b','8952b70e6da7ff9b4906eff0b5e3a126774809bc218b2ef3bb9c6e5c2c675f6b','V006__scheduler_reservations','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.341139');
INSERT INTO "_yoyo_log" VALUES('9c3bd0e8-208e-40be-8e3a-30f5f4a2e408','e5e4065a0b057e695b3714874a89d1eefee32992a5413d3a20c87d33047c3868','V007__session_cost_source_rollup','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.345416');
INSERT INTO "_yoyo_log" VALUES('e243fd48-fd5f-4f28-b2fc-840c9205051b','4d3e53e28b07c15f4376c1c29fffce510998a6168f8813de4e77ad2f349dcb0e','V008__scheduler_job_tool_policy','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.348967');
INSERT INTO "_yoyo_log" VALUES('bd026ab5-cacb-4645-91c5-27ca5ecf25aa','48dc08849da7082cee9fb31705f191c903e954424217d79853458b4f44009139','V009__transcript_reasoning_content','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.352022');
INSERT INTO "_yoyo_log" VALUES('89d1c2e7-e0fa-44af-a5b9-04c61797b81b','b8fa71f746d6fa9c9ed7a53431dbf461981aee112b5f6d359a2ee7ce567e7ae0','V010__meta_skill_runs','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.359148');
INSERT INTO "_yoyo_log" VALUES('3c093ba7-b8d1-492f-a4cb-a4b9d2ab96ba','e6baa87a081e3b02b468e572ec56ce1143305971359de45a14c0e68757948668','V010__transcript_turn_usage','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.362127');
INSERT INTO "_yoyo_log" VALUES('449c5891-5169-41d8-9a7c-cf28faba61d8','d90c228ae7e7bc7a538047155c84246f58c969cbcea733632678fd7596a792a2','V011__meta_skill_runs_triggered_by_auto','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.372133');
INSERT INTO "_yoyo_log" VALUES('61775d7b-404e-46fa-9829-b1f35371f8c7','cea1bccbed0a87f37701530f74b5ad508c48fe6da4c28e3fb1efd4ed792f071b','V012__meta_skill_run_steps_allow_llm_chat','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.377833');
INSERT INTO "_yoyo_log" VALUES('3454ce29-6c63-47b0-b328-06571e0f1b73','f056b89cfd71e19a17af4319f0082930e8de9f046bf456213a747afca4746f15','V013__meta_skill_runs_clarify','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.377833');
INSERT INTO "_yoyo_log" VALUES('61793afa-1ea6-4a0f-ae3c-156c67f1a32e','54c1e21aeb9fc554d58c680ae879bf7ee1f41fb3b6fdae45b6b61e9302091e4e','V014__meta_skill_run_steps_allow_user_input','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.394656');
INSERT INTO "_yoyo_log" VALUES('a05bb5f4-138c-4909-be4c-fea8d82d4a56','0f2fd2e6c3b4474ff4311c4dfa22894c3cd7af14e74b7d417289897e40f1c33f','V015__meta_skill_step_usage','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.402824');
INSERT INTO "_yoyo_log" VALUES('89291cfc-6aa4-417f-ac9b-fd4dbbdde6af','5ab0a762718508de688c19d227fe5885b0497e1737a3b2abe2af9f079d75acc4','V016__meta_skill_runs_triggered_by_manual_command','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.411019');
INSERT INTO "_yoyo_log" VALUES('de0ca079-f860-446a-bd0d-a372df1e85a5','761b244747cdb20a6a8bca3afcd29ae6ce28618136626f5b14c08eb4905e9e60','V017__router_decisions','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.412645');
INSERT INTO "_yoyo_log" VALUES('800a263f-d297-4923-8660-9510a797b001','2a1d1375a006bebda94655c0396eec40a5cb07761c7da2d2380e90aba4e268d1','V018__router_decisions_ts_index','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.412645');
INSERT INTO "_yoyo_log" VALUES('a3012bf3-cce4-48c1-8629-2166df6b0c60','9ae7a6ed59553c6b8610135be7d769a471aaf9b22d49f525aa5142df87ad0955','V019__turn_errors','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.428013');
INSERT INTO "_yoyo_log" VALUES('7f7b846b-f418-4eaf-a3cc-6025f1956be8','383a9e656005400232e4c152a7ba9d6dabf45439e2f6f2916a5c2037ccec4d79','V020__turn_ingress_receipts','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.435106');
INSERT INTO "_yoyo_log" VALUES('adcef074-6dba-4787-89b0-dce46ea6c01a','bb6a16fb23923afe24f6ffd5fd82aaed1009a3dea838316a64f7015f51d5ce13','V021__usage_ledger','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.441535');
INSERT INTO "_yoyo_log" VALUES('e1b1bd97-efe8-4655-b6b4-b36451ba2b3f','a3da327dec37247a4146025632c3ce3e7c115ad1a1fca96e72a4a51e945d3c5f','V022__telemetry_daily_usage','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.448062');
INSERT INTO "_yoyo_log" VALUES('9095fc81-5edb-49e2-87be-bc65000cde74','fff2b0ba4b586319c9f6f28d407fa1a9533843e4e33b5cc98ae86e416d9bfba4','V023__router_deployment_telemetry','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.455438');
INSERT INTO "_yoyo_log" VALUES('b8e8f35b-e355-4a79-9369-108946f4aafd','188a1afee3887b9cc282a5773da3eede55c8e7032883709e2e8b1637bc3fa10e','V024__usage_native_billing_receipts','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.462642');
INSERT INTO "_yoyo_log" VALUES('d98c5325-4162-4cf8-9bb3-78a8c0daffc1','5db75a17c3fda810a17977a252e9c1c71914f753d857638230acf68ad84ca7d3','V025__session_collaboration_state','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.467732');
INSERT INTO "_yoyo_log" VALUES('5c1f2e70-3ec1-413e-aa09-e207976516ea','10d74d6275d190418625b5dd46752cf49010130b80e7e3fe632e1fffc6bb3701','V026__plan_revisions','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.474395');
INSERT INTO "_yoyo_log" VALUES('ebf23791-a167-4e00-9cd1-19ed443b6cfe','a170daebac8938324d00d5f8848760213e8b30991b3f01ac1e426443a7dce8d9','V027__plan_runs','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.477859');
INSERT INTO "_yoyo_log" VALUES('5da6cd50-28c3-470a-98ba-f57505965fd8','5fe559261a44ffacb5df4dee2667e6bad606bde6dac301e36800c16714ff74a5','V028__project_workspaces','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.487457');
INSERT INTO "_yoyo_log" VALUES('cd90f11b-f4e8-49af-a733-d2eaf96ea21e','bb45c37e946b797b56b04c84597b3b9d08478d9d2263aaad0d86e832f8821383','V029__sandbox_policy_tokens','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.495478');
INSERT INTO "_yoyo_log" VALUES('a1c14b05-ceb7-4a38-a4de-ded57fd25c1b','15bedd59a5e46186705059209844432a1b02d405da83a450856634985989f022','V030__meta_control_intents','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.501617');
INSERT INTO "_yoyo_log" VALUES('59953c25-b454-4e33-812f-aea3d49e86ef','3a12ae5f000071055b1c020c88beddbf19b01eac4dd063dca6ac29e4ce0dbe3b','V031__meta_launch_drafts','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.509142');
INSERT INTO "_yoyo_log" VALUES('02b3657a-98ed-47ce-81fe-ecb9180726dc','5b2e2e3bec083010f20eb6e9db08d55cd70b87712fdffb0ad2a8c6913b7c5a60','V032__meta_launch_discard_tombstones','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.512763');
INSERT INTO "_yoyo_log" VALUES('bc9624e9-9436-4f14-8e5c-d8edaedbcf14','52d8127dbdbe6d90f879c2ced815e7ea6e45b9c439709b66f668d326eb924081','V033__goal_runs','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.512763');
INSERT INTO "_yoyo_log" VALUES('fd2f99c4-ddb6-4bc4-9808-f23e58a21eb1','95787682c392ecc7be85fb840c51c014ad9b286263b05a47d5a63c44b6dd6e1a','V034__goal_message_anchor','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.530875');
INSERT INTO "_yoyo_log" VALUES('3fcf106c-03d6-4251-8697-19baa2ae590a','d49af6f58d176ee6cf74e2bb1974eabd0bee0f2866bf10c6bb4b149eeaa5d057','V035__pending_chat_inputs','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.533765');
INSERT INTO "_yoyo_log" VALUES('23f92e11-890c-4b3b-af90-bca5333c99dc','0f69476f0f367b5afa59cd4dc801b9db07aa09515050081dd72241d7a4dc74bf','V036__session_model_routing','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.543058');
INSERT INTO "_yoyo_log" VALUES('474be51e-9463-4cae-9427-1385292722c5','ce6e395596f5baa0ffa504c56bfd16f515d2fcc0569df92fbfe1eef51756b51c','V037__artifact_sessions','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.549222');
INSERT INTO "_yoyo_log" VALUES('49d9fc0a-740c-4d33-b946-e9a1d0aafffe','09bcc9c4698eaa5e9de48762aab706514fb9c9f53bee51b72155eb6e2f4c1d1d','V038__artifact_prompt_annotations','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.557200');
INSERT INTO "_yoyo_log" VALUES('c2a8d059-b4a3-48c0-92fa-841dcf75ce5e','bff7a977a8e3a5f5c1a4b4bcbae0c72669f55a09d9554b61d2a52525db3d0010','V039__artifact_mutation_attempts','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.565737');
INSERT INTO "_yoyo_log" VALUES('cc6dc731-775c-444f-8074-4a1fedf9ac8f','fdeab5dfd8c3813b9167a01fc08b14f2a718b6fe3da6cd8b6bb14b2df90c8a62','V040__document_resources','apply','upgrade-fixture','fixture-host',NULL,'2026-09-10T18:06:15.572312');
CREATE TABLE "_yoyo_migration" (
            "migration_hash" VARCHAR(64),
            "migration_id" VARCHAR(255),
            "applied_at_utc" TIMESTAMP,
            PRIMARY KEY ("migration_hash")
        );
INSERT INTO "_yoyo_migration" VALUES('f8a5255cbc8d7094d88289b6e1dd02bb73fbffbdf97ced47d88799ceb4cfde6e','V001__initial_schema','2026-09-10T18:06:15.319398');
INSERT INTO "_yoyo_migration" VALUES('335ac2e449bfe0f30bec8a0308c76b51ec10f480ec88aeef7250ce769e8a70ee','V002__scheduler_session_fields','2026-09-10T18:06:15.322359');
INSERT INTO "_yoyo_migration" VALUES('2732174eb02055fd93931e860feaa8b68879cbe1cb984f7d3616d5392f75bb8e','V003__heartbeat_ticks','2026-09-10T18:06:15.329368');
INSERT INTO "_yoyo_migration" VALUES('3a223b7c95ec8c0bbfd9064e8b766d225788f03f761dd9f5081ef0eef7295f6b','V004__memory_schema_version','2026-09-10T18:06:15.333385');
INSERT INTO "_yoyo_migration" VALUES('8480378c8c5f6f322c1707a8b01f56d67a8b392ea67b6f8e199b2b36aeb1468f','V005__agent_tasks','2026-09-10T18:06:15.338377');
INSERT INTO "_yoyo_migration" VALUES('8952b70e6da7ff9b4906eff0b5e3a126774809bc218b2ef3bb9c6e5c2c675f6b','V006__scheduler_reservations','2026-09-10T18:06:15.343145');
INSERT INTO "_yoyo_migration" VALUES('e5e4065a0b057e695b3714874a89d1eefee32992a5413d3a20c87d33047c3868','V007__session_cost_source_rollup','2026-09-10T18:06:15.346963');
INSERT INTO "_yoyo_migration" VALUES('4d3e53e28b07c15f4376c1c29fffce510998a6168f8813de4e77ad2f349dcb0e','V008__scheduler_job_tool_policy','2026-09-10T18:06:15.350967');
INSERT INTO "_yoyo_migration" VALUES('48dc08849da7082cee9fb31705f191c903e954424217d79853458b4f44009139','V009__transcript_reasoning_content','2026-09-10T18:06:15.354137');
INSERT INTO "_yoyo_migration" VALUES('b8fa71f746d6fa9c9ed7a53431dbf461981aee112b5f6d359a2ee7ce567e7ae0','V010__meta_skill_runs','2026-09-10T18:06:15.362127');
INSERT INTO "_yoyo_migration" VALUES('e6baa87a081e3b02b468e572ec56ce1143305971359de45a14c0e68757948668','V010__transcript_turn_usage','2026-09-10T18:06:15.362127');
INSERT INTO "_yoyo_migration" VALUES('d90c228ae7e7bc7a538047155c84246f58c969cbcea733632678fd7596a792a2','V011__meta_skill_runs_triggered_by_auto','2026-09-10T18:06:15.374836');
INSERT INTO "_yoyo_migration" VALUES('cea1bccbed0a87f37701530f74b5ad508c48fe6da4c28e3fb1efd4ed792f071b','V012__meta_skill_run_steps_allow_llm_chat','2026-09-10T18:06:15.377833');
INSERT INTO "_yoyo_migration" VALUES('f056b89cfd71e19a17af4319f0082930e8de9f046bf456213a747afca4746f15','V013__meta_skill_runs_clarify','2026-09-10T18:06:15.377833');
INSERT INTO "_yoyo_migration" VALUES('54c1e21aeb9fc554d58c680ae879bf7ee1f41fb3b6fdae45b6b61e9302091e4e','V014__meta_skill_run_steps_allow_user_input','2026-09-10T18:06:15.398818');
INSERT INTO "_yoyo_migration" VALUES('0f2fd2e6c3b4474ff4311c4dfa22894c3cd7af14e74b7d417289897e40f1c33f','V015__meta_skill_step_usage','2026-09-10T18:06:15.404826');
INSERT INTO "_yoyo_migration" VALUES('5ab0a762718508de688c19d227fe5885b0497e1737a3b2abe2af9f079d75acc4','V016__meta_skill_runs_triggered_by_manual_command','2026-09-10T18:06:15.412645');
INSERT INTO "_yoyo_migration" VALUES('761b244747cdb20a6a8bca3afcd29ae6ce28618136626f5b14c08eb4905e9e60','V017__router_decisions','2026-09-10T18:06:15.412645');
INSERT INTO "_yoyo_migration" VALUES('2a1d1375a006bebda94655c0396eec40a5cb07761c7da2d2380e90aba4e268d1','V018__router_decisions_ts_index','2026-09-10T18:06:15.423932');
INSERT INTO "_yoyo_migration" VALUES('9ae7a6ed59553c6b8610135be7d769a471aaf9b22d49f525aa5142df87ad0955','V019__turn_errors','2026-09-10T18:06:15.428013');
INSERT INTO "_yoyo_migration" VALUES('383a9e656005400232e4c152a7ba9d6dabf45439e2f6f2916a5c2037ccec4d79','V020__turn_ingress_receipts','2026-09-10T18:06:15.435106');
INSERT INTO "_yoyo_migration" VALUES('bb6a16fb23923afe24f6ffd5fd82aaed1009a3dea838316a64f7015f51d5ce13','V021__usage_ledger','2026-09-10T18:06:15.443543');
INSERT INTO "_yoyo_migration" VALUES('a3da327dec37247a4146025632c3ce3e7c115ad1a1fca96e72a4a51e945d3c5f','V022__telemetry_daily_usage','2026-09-10T18:06:15.450398');
INSERT INTO "_yoyo_migration" VALUES('fff2b0ba4b586319c9f6f28d407fa1a9533843e4e33b5cc98ae86e416d9bfba4','V023__router_deployment_telemetry','2026-09-10T18:06:15.457638');
INSERT INTO "_yoyo_migration" VALUES('188a1afee3887b9cc282a5773da3eede55c8e7032883709e2e8b1637bc3fa10e','V024__usage_native_billing_receipts','2026-09-10T18:06:15.466188');
INSERT INTO "_yoyo_migration" VALUES('5db75a17c3fda810a17977a252e9c1c71914f753d857638230acf68ad84ca7d3','V025__session_collaboration_state','2026-09-10T18:06:15.469971');
INSERT INTO "_yoyo_migration" VALUES('10d74d6275d190418625b5dd46752cf49010130b80e7e3fe632e1fffc6bb3701','V026__plan_revisions','2026-09-10T18:06:15.477859');
INSERT INTO "_yoyo_migration" VALUES('a170daebac8938324d00d5f8848760213e8b30991b3f01ac1e426443a7dce8d9','V027__plan_runs','2026-09-10T18:06:15.477859');
INSERT INTO "_yoyo_migration" VALUES('5fe559261a44ffacb5df4dee2667e6bad606bde6dac301e36800c16714ff74a5','V028__project_workspaces','2026-09-10T18:06:15.490540');
INSERT INTO "_yoyo_migration" VALUES('bb45c37e946b797b56b04c84597b3b9d08478d9d2263aaad0d86e832f8821383','V029__sandbox_policy_tokens','2026-09-10T18:06:15.495478');
INSERT INTO "_yoyo_migration" VALUES('15bedd59a5e46186705059209844432a1b02d405da83a450856634985989f022','V030__meta_control_intents','2026-09-10T18:06:15.504492');
INSERT INTO "_yoyo_migration" VALUES('3a12ae5f000071055b1c020c88beddbf19b01eac4dd063dca6ac29e4ce0dbe3b','V031__meta_launch_drafts','2026-09-10T18:06:15.511681');
INSERT INTO "_yoyo_migration" VALUES('5b2e2e3bec083010f20eb6e9db08d55cd70b87712fdffb0ad2a8c6913b7c5a60','V032__meta_launch_discard_tombstones','2026-09-10T18:06:15.512763');
INSERT INTO "_yoyo_migration" VALUES('52d8127dbdbe6d90f879c2ced815e7ea6e45b9c439709b66f668d326eb924081','V033__goal_runs','2026-09-10T18:06:15.523892');
INSERT INTO "_yoyo_migration" VALUES('95787682c392ecc7be85fb840c51c014ad9b286263b05a47d5a63c44b6dd6e1a','V034__goal_message_anchor','2026-09-10T18:06:15.531381');
INSERT INTO "_yoyo_migration" VALUES('d49af6f58d176ee6cf74e2bb1974eabd0bee0f2866bf10c6bb4b149eeaa5d057','V035__pending_chat_inputs','2026-09-10T18:06:15.533765');
INSERT INTO "_yoyo_migration" VALUES('0f69476f0f367b5afa59cd4dc801b9db07aa09515050081dd72241d7a4dc74bf','V036__session_model_routing','2026-09-10T18:06:15.544425');
INSERT INTO "_yoyo_migration" VALUES('ce6e395596f5baa0ffa504c56bfd16f515d2fcc0569df92fbfe1eef51756b51c','V037__artifact_sessions','2026-09-10T18:06:15.549222');
INSERT INTO "_yoyo_migration" VALUES('09bcc9c4698eaa5e9de48762aab706514fb9c9f53bee51b72155eb6e2f4c1d1d','V038__artifact_prompt_annotations','2026-09-10T18:06:15.560420');
INSERT INTO "_yoyo_migration" VALUES('bff7a977a8e3a5f5c1a4b4bcbae0c72669f55a09d9554b61d2a52525db3d0010','V039__artifact_mutation_attempts','2026-09-10T18:06:15.567685');
INSERT INTO "_yoyo_migration" VALUES('fdeab5dfd8c3813b9167a01fc08b14f2a718b6fe3da6cd8b6bb14b2df90c8a62','V040__document_resources','2026-09-10T18:06:15.573837');
CREATE TABLE "_yoyo_version" (
            "version" INT NOT NULL PRIMARY KEY,
            "installed_at_utc" TIMESTAMP
        );
INSERT INTO "_yoyo_version" VALUES(2,'2026-09-10 18:06:14.993717');
CREATE TABLE agent_tasks (
    task_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    agent_id TEXT NOT NULL DEFAULT 'main',
    source_kind TEXT NOT NULL,
    queue_mode TEXT NOT NULL,
    run_kind TEXT NOT NULL DEFAULT 'default',
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    terminal_reason TEXT,
    error_class TEXT,
    error_message TEXT,
    details TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE artifact_anchors (
        anchor_id                TEXT PRIMARY KEY,
        document_id              TEXT NOT NULL,
        revision_id              TEXT NOT NULL,
        kind                     TEXT NOT NULL,
        locator_json             TEXT NOT NULL,
        quote                    TEXT,
        context_json             TEXT,
        state                    TEXT NOT NULL,
        remapped_from_anchor_id  TEXT,
        created_at               INTEGER NOT NULL,
        schema_version           INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE,
        FOREIGN KEY (revision_id) REFERENCES artifact_revisions(revision_id),
        FOREIGN KEY (remapped_from_anchor_id) REFERENCES artifact_anchors(anchor_id)
    );
CREATE TABLE artifact_audit_events (
        sequence        INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id        TEXT NOT NULL UNIQUE,
        document_id     TEXT NOT NULL,
        event_type      TEXT NOT NULL,
        actor_kind      TEXT NOT NULL,
        actor_id        TEXT NOT NULL,
        revision_id     TEXT,
        change_set_id   TEXT,
        anchor_id       TEXT,
        edit_session_id TEXT,
        lease_id        TEXT,
        payload_json    TEXT NOT NULL,
        created_at      INTEGER NOT NULL,
        schema_version  INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE
    );
CREATE TABLE artifact_change_sets (
        change_set_id             TEXT PRIMARY KEY,
        document_id               TEXT NOT NULL,
        base_revision_id          TEXT NOT NULL,
        turn_id                   TEXT,
        summary                   TEXT NOT NULL DEFAULT '',
        status                    TEXT NOT NULL,
        operations_json           TEXT NOT NULL,
        candidate_artifact_id     TEXT,
        candidate_artifact_sha256 TEXT,
        candidate_filename        TEXT,
        candidate_media_type      TEXT,
        candidate_byte_size       INTEGER CHECK (
            candidate_byte_size IS NULL OR candidate_byte_size >= 0
        ),
        validation_json           TEXT,
        state_revision            INTEGER NOT NULL CHECK (state_revision >= 1),
        created_by_kind           TEXT NOT NULL,
        created_by_id             TEXT NOT NULL,
        applied_revision_id       TEXT,
        created_at                INTEGER NOT NULL,
        updated_at                INTEGER NOT NULL,
        schema_version            INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE,
        FOREIGN KEY (base_revision_id) REFERENCES artifact_revisions(revision_id),
        FOREIGN KEY (applied_revision_id) REFERENCES artifact_revisions(revision_id)
    );
CREATE TABLE artifact_documents (
        document_id            TEXT PRIMARY KEY,
        session_key            TEXT NOT NULL,
        session_id             TEXT,
        name                   TEXT NOT NULL,
        kind                   TEXT NOT NULL,
        head_revision_id       TEXT NOT NULL,
        generation             INTEGER NOT NULL CHECK (generation >= 1),
        state_revision         INTEGER NOT NULL CHECK (state_revision >= 1),
        writer_fencing_token   INTEGER NOT NULL DEFAULT 0
                               CHECK (writer_fencing_token >= 0),
        created_at             INTEGER NOT NULL,
        updated_at             INTEGER NOT NULL,
        schema_version         INTEGER NOT NULL DEFAULT 1
    );
CREATE TABLE artifact_edit_sessions (
        edit_session_id        TEXT PRIMARY KEY,
        document_id            TEXT NOT NULL,
        base_revision_id       TEXT NOT NULL,
        last_saved_revision_id TEXT NOT NULL,
        mode                   TEXT NOT NULL,
        status                 TEXT NOT NULL,
        user_id                TEXT NOT NULL,
        state_revision         INTEGER NOT NULL CHECK (state_revision >= 1),
        expires_at             INTEGER NOT NULL,
        last_access_at         INTEGER NOT NULL,
        created_at             INTEGER NOT NULL,
        updated_at             INTEGER NOT NULL,
        schema_version         INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE,
        FOREIGN KEY (base_revision_id) REFERENCES artifact_revisions(revision_id),
        FOREIGN KEY (last_saved_revision_id) REFERENCES artifact_revisions(revision_id)
    );
CREATE TABLE artifact_mutation_attempts (
        mutation_attempt_id TEXT PRIMARY KEY,
        document_id         TEXT NOT NULL,
        turn_id             TEXT NOT NULL,
        tool_use_id         TEXT NOT NULL,
        base_revision_id    TEXT NOT NULL,
        proposal_sha256     TEXT
                            CHECK (
                                proposal_sha256 IS NULL
                                OR (
                                    length(proposal_sha256) = 64
                                    AND proposal_sha256 = lower(proposal_sha256)
                                    AND proposal_sha256 NOT GLOB '*[^0-9a-f]*'
                                )
                            ),
        status              TEXT NOT NULL
                            CHECK (status IN ('reserved', 'applied', 'failed', 'ambiguous')),
        change_set_id       TEXT,
        revision_id         TEXT,
        failure_code        TEXT
                            CHECK (
                                failure_code IS NULL
                                OR length(CAST(failure_code AS BLOB)) <= 128
                            ),
        candidate_session_id      TEXT,
        candidate_artifact_id     TEXT,
        candidate_artifact_sha256 TEXT,
        candidate_registered_at   INTEGER,
        state_revision      INTEGER NOT NULL CHECK (state_revision >= 1),
        created_at          INTEGER NOT NULL,
        updated_at          INTEGER NOT NULL,
        schema_version      INTEGER NOT NULL DEFAULT 1,
        UNIQUE (turn_id),
        CHECK (
            (status = 'reserved' AND change_set_id IS NULL
             AND revision_id IS NULL AND failure_code IS NULL)
            OR
            (status = 'applied' AND change_set_id IS NOT NULL
             AND revision_id IS NOT NULL AND failure_code IS NULL)
            OR
            (status IN ('failed', 'ambiguous') AND failure_code IS NOT NULL)
        ),
        CHECK (
            (candidate_session_id IS NULL AND candidate_artifact_id IS NULL
             AND candidate_artifact_sha256 IS NULL AND candidate_registered_at IS NULL)
            OR
            (candidate_session_id IS NOT NULL AND candidate_artifact_id IS NOT NULL
             AND candidate_artifact_sha256 IS NOT NULL
             AND candidate_registered_at IS NOT NULL)
        ),
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE,
        FOREIGN KEY (base_revision_id) REFERENCES artifact_revisions(revision_id),
        FOREIGN KEY (change_set_id) REFERENCES artifact_change_sets(change_set_id),
        FOREIGN KEY (revision_id) REFERENCES artifact_revisions(revision_id)
    );
CREATE TABLE artifact_prompt_annotations (
        annotation_id   TEXT PRIMARY KEY,
        session_key     TEXT NOT NULL,
        session_id      TEXT NOT NULL,
        session_epoch   INTEGER NOT NULL CHECK (session_epoch >= 0),
        document_id     TEXT NOT NULL,
        revision_id     TEXT NOT NULL,
        anchor_id       TEXT NOT NULL,
        body            TEXT NOT NULL
                        CHECK (length(CAST(body AS BLOB)) <= 16384),
        status          TEXT NOT NULL
                        CHECK (status IN ('draft', 'sent', 'discarded')),
        state_revision  INTEGER NOT NULL CHECK (state_revision >= 1),
        sent_message_id TEXT,
        sent_turn_id    TEXT,
        sent_order      INTEGER CHECK (sent_order IS NULL OR sent_order >= 0),
        created_at      INTEGER NOT NULL,
        updated_at      INTEGER NOT NULL,
        schema_version  INTEGER NOT NULL DEFAULT 1,
        CHECK (
            (status = 'sent' AND sent_message_id IS NOT NULL
             AND sent_turn_id IS NOT NULL AND sent_order IS NOT NULL)
            OR
            (status IN ('draft', 'discarded') AND sent_message_id IS NULL
             AND sent_turn_id IS NULL AND sent_order IS NULL)
        ),
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE,
        FOREIGN KEY (revision_id) REFERENCES artifact_revisions(revision_id),
        FOREIGN KEY (anchor_id) REFERENCES artifact_anchors(anchor_id)
    );
CREATE TABLE artifact_revisions (
        revision_id             TEXT PRIMARY KEY,
        document_id             TEXT NOT NULL,
        parent_revision_id      TEXT,
        generation              INTEGER NOT NULL CHECK (generation >= 1),
        artifact_id             TEXT NOT NULL,
        artifact_sha256         TEXT NOT NULL,
        filename                TEXT NOT NULL,
        media_type              TEXT NOT NULL,
        byte_size               INTEGER NOT NULL CHECK (byte_size >= 0),
        source                  TEXT NOT NULL,
        actor_kind              TEXT NOT NULL,
        actor_id                TEXT NOT NULL,
        change_set_id           TEXT,
        copied_from_revision_id TEXT,
        created_at              INTEGER NOT NULL,
        schema_version          INTEGER NOT NULL DEFAULT 1,
        UNIQUE (document_id, generation),
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE,
        FOREIGN KEY (parent_revision_id) REFERENCES artifact_revisions(revision_id)
    );
CREATE TABLE artifact_writer_leases (
        document_id    TEXT PRIMARY KEY,
        lease_id       TEXT NOT NULL UNIQUE,
        holder_id      TEXT NOT NULL,
        fencing_token  INTEGER NOT NULL CHECK (fencing_token >= 1),
        expires_at     INTEGER NOT NULL,
        created_at     INTEGER NOT NULL,
        updated_at     INTEGER NOT NULL,
        schema_version INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE
    );
CREATE TABLE document_import_attempts (
        attempt_id            TEXT PRIMARY KEY,
        session_key           TEXT NOT NULL,
        session_id            TEXT NOT NULL,
        idempotency_key       TEXT NOT NULL,
        source_type           TEXT NOT NULL CHECK (source_type IN ('attachment', 'deliverable')),
        source_resource_id    TEXT NOT NULL,
        source_sha256         TEXT NOT NULL CHECK (length(source_sha256) = 64),
        source_name           TEXT NOT NULL,
        source_mime           TEXT NOT NULL,
        source_size           INTEGER NOT NULL CHECK (source_size >= 0),
        document_name         TEXT NOT NULL,
        mode                  TEXT NOT NULL CHECK (mode = 'copy'),
        candidate_artifact_id TEXT NOT NULL UNIQUE,
        status                TEXT NOT NULL
                              CHECK (status IN ('reserved', 'applied', 'failed', 'ambiguous')),
        document_id           TEXT,
        revision_id           TEXT,
        binding_id            TEXT,
        failure_code          TEXT
                              CHECK (failure_code IS NULL OR length(failure_code) <= 128),
        candidate_cleaned_at  INTEGER,
        state_revision        INTEGER NOT NULL CHECK (state_revision >= 1),
        created_at            INTEGER NOT NULL,
        updated_at            INTEGER NOT NULL,
        schema_version        INTEGER NOT NULL DEFAULT 1,
        UNIQUE (session_id, idempotency_key),
        CHECK (
            (status = 'reserved' AND document_id IS NULL AND revision_id IS NULL
             AND binding_id IS NULL AND failure_code IS NULL)
            OR
            (status = 'applied' AND document_id IS NOT NULL AND revision_id IS NOT NULL
             AND binding_id IS NOT NULL AND failure_code IS NULL)
            OR
            (status IN ('failed', 'ambiguous') AND failure_code IS NOT NULL)
        ),
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE,
        FOREIGN KEY (revision_id) REFERENCES artifact_revisions(revision_id),
        FOREIGN KEY (binding_id) REFERENCES document_source_bindings(binding_id)
    );
CREATE TABLE document_publications (
        publication_id         TEXT PRIMARY KEY,
        session_key            TEXT NOT NULL,
        session_id             TEXT NOT NULL,
        document_id            TEXT NOT NULL,
        revision_id            TEXT NOT NULL,
        deliverable_artifact_id TEXT NOT NULL UNIQUE,
        artifact_sha256        TEXT NOT NULL CHECK (length(artifact_sha256) = 64),
        name                   TEXT NOT NULL,
        mime                   TEXT NOT NULL,
        size                   INTEGER NOT NULL CHECK (size >= 0),
        created_by_kind        TEXT NOT NULL,
        created_by_id          TEXT NOT NULL,
        created_at             INTEGER NOT NULL,
        schema_version         INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE,
        FOREIGN KEY (revision_id) REFERENCES artifact_revisions(revision_id)
    );
CREATE TABLE document_publish_attempts (
        attempt_id             TEXT PRIMARY KEY,
        session_key            TEXT NOT NULL,
        session_id             TEXT NOT NULL,
        idempotency_key        TEXT NOT NULL,
        document_id            TEXT NOT NULL,
        revision_id            TEXT NOT NULL,
        candidate_artifact_id  TEXT NOT NULL UNIQUE,
        artifact_sha256        TEXT NOT NULL CHECK (length(artifact_sha256) = 64),
        name                   TEXT NOT NULL,
        mime                   TEXT NOT NULL,
        size                   INTEGER NOT NULL CHECK (size >= 0),
        status                 TEXT NOT NULL
                               CHECK (status IN ('reserved', 'applied', 'failed', 'ambiguous')),
        publication_id         TEXT,
        deliverable_artifact_id TEXT,
        failure_code           TEXT
                               CHECK (failure_code IS NULL OR length(failure_code) <= 128),
        promoted_at            INTEGER,
        state_revision         INTEGER NOT NULL CHECK (state_revision >= 1),
        created_at             INTEGER NOT NULL,
        updated_at             INTEGER NOT NULL,
        schema_version         INTEGER NOT NULL DEFAULT 1,
        UNIQUE (session_id, idempotency_key),
        CHECK (
            (status = 'reserved' AND publication_id IS NULL
             AND deliverable_artifact_id IS NULL AND failure_code IS NULL)
            OR
            (status = 'applied' AND publication_id IS NOT NULL
             AND deliverable_artifact_id IS NOT NULL AND failure_code IS NULL)
            OR
            (status IN ('failed', 'ambiguous') AND failure_code IS NOT NULL)
        ),
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE,
        FOREIGN KEY (revision_id) REFERENCES artifact_revisions(revision_id),
        FOREIGN KEY (publication_id) REFERENCES document_publications(publication_id)
    );
CREATE TABLE document_source_bindings (
        binding_id         TEXT PRIMARY KEY,
        document_id        TEXT NOT NULL UNIQUE,
        session_key        TEXT NOT NULL,
        session_id         TEXT NOT NULL,
        source_type        TEXT NOT NULL CHECK (source_type IN ('attachment', 'deliverable')),
        source_resource_id TEXT NOT NULL,
        source_sha256      TEXT NOT NULL CHECK (length(source_sha256) = 64),
        source_name        TEXT NOT NULL,
        source_mime        TEXT NOT NULL,
        source_size        INTEGER NOT NULL CHECK (source_size >= 0),
        mode               TEXT NOT NULL CHECK (mode = 'copy'),
        created_at         INTEGER NOT NULL,
        schema_version     INTEGER NOT NULL DEFAULT 1,
        UNIQUE (session_id, source_type, source_resource_id),
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE
    );
CREATE TABLE goal_command_receipts (
    receipt_id TEXT PRIMARY KEY,
    source_scope TEXT NOT NULL,
    request_session_key TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    action TEXT NOT NULL
        CHECK (action IN ('set', 'edit', 'pause', 'resume', 'clear')),
    request_fingerprint TEXT NOT NULL,
    accepted_session_id TEXT NOT NULL,
    accepted_session_epoch INTEGER NOT NULL DEFAULT 0
        CHECK (accepted_session_epoch >= 0),
    response_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    FOREIGN KEY (request_session_key) REFERENCES sessions(session_key) ON DELETE CASCADE
);
CREATE TABLE heartbeat_ticks (
    id TEXT PRIMARY KEY,
    emitted_at TEXT NOT NULL,
    priority_band TEXT NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 0,
    schema_version INTEGER NOT NULL DEFAULT 1,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE meta_control_intents (
    intent_id                           TEXT PRIMARY KEY,
    session_key                        TEXT NOT NULL,
    control_kind                       TEXT NOT NULL,
    correlation_id                     TEXT NOT NULL,
    meta_skill_name                    TEXT NOT NULL,
    replay_run_id                      TEXT,
    replay_mode                        TEXT,
    status                             TEXT NOT NULL DEFAULT 'staged',
    accepted_source_scope              TEXT,
    accepted_request_session_key       TEXT,
    accepted_client_request_id         TEXT,
    accepted_request_fingerprint       TEXT,
    accepted_message_id                TEXT,
    accepted_task_id                   TEXT,
    created_at                         INTEGER NOT NULL,
    updated_at                         INTEGER NOT NULL,
    schema_version                     INTEGER NOT NULL DEFAULT 1,
    CHECK (control_kind IN ('manual', 'replay')),
    CHECK (status IN ('staged', 'accepted'))
);
CREATE TABLE meta_launch_discard_tombstones (
    session_key         TEXT NOT NULL,
    client_request_id   TEXT NOT NULL,
    created_at          INTEGER NOT NULL,
    expires_at          INTEGER NOT NULL,
    schema_version      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (session_key, client_request_id)
);
CREATE TABLE meta_launch_drafts (
    draft_id            TEXT PRIMARY KEY,
    session_key         TEXT NOT NULL,
    client_request_id   TEXT NOT NULL,
    meta_skill_name     TEXT NOT NULL,
    launch_text         TEXT NOT NULL,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    expires_at          INTEGER NOT NULL,
    schema_version      INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE "meta_skill_run_steps" (
        run_id              TEXT NOT NULL
                              REFERENCES meta_skill_runs(run_id) ON DELETE CASCADE,
        step_id             TEXT NOT NULL,
        step_kind           TEXT NOT NULL
                              CHECK(step_kind IN ('agent', 'llm_classify', 'llm_chat', 'tool_call', 'skill_exec', 'user_input')),
        declared_skill      TEXT NOT NULL,
        effective_skill     TEXT NOT NULL,
        status              TEXT NOT NULL
                              CHECK(status IN ('running','ok','failed','substituted')),
        started_at_ms       INTEGER NOT NULL,
        ended_at_ms         INTEGER,
        rendered_inputs_json TEXT NOT NULL,
        output_text         TEXT,
        error               TEXT,
        substitute_step_id  TEXT,
        truncated_fields    TEXT NOT NULL DEFAULT '', usage_json TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (run_id, step_id)
    );
CREATE TABLE "meta_skill_runs" (
        run_id                 TEXT PRIMARY KEY,
        meta_skill_name        TEXT NOT NULL,
        meta_skill_digest      TEXT NOT NULL,
        plan_snapshot_json     TEXT NOT NULL,
        triggered_by           TEXT NOT NULL
                                 CHECK(triggered_by IN (
                                     'hard_takeover', 'soft_meta_invoke', 'auto_cron', 'auto_dream', 'manual_command'
                                 )),
        session_key            TEXT,
        turn_id                TEXT,
        owner_pid              INTEGER,
        status                 TEXT NOT NULL
                                 CHECK(status IN ('running', 'ok', 'failed', 'cancelled', 'awaiting_user', 'expired')),
        started_at_ms          INTEGER NOT NULL,
        ended_at_ms            INTEGER,
        inputs_json            TEXT NOT NULL,
        final_text             TEXT,
        failed_step_id         TEXT,
        error                  TEXT,
        truncated_fields       TEXT NOT NULL DEFAULT '',
        awaiting_step_id       TEXT,
        awaiting_schema_json   TEXT,
        awaiting_since         REAL,
        awaiting_filled_json   TEXT,
        step_outputs_json      TEXT,
        parse_failure_count    INTEGER NOT NULL DEFAULT 0
    );
CREATE TABLE pending_chat_input_cancellations (
    pending_input_id       TEXT PRIMARY KEY,
    session_key            TEXT NOT NULL,
    cancelled_at           INTEGER NOT NULL,
    schema_version         INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1)
);
CREATE TABLE pending_chat_input_dispatch_receipts (
    pending_input_id       TEXT PRIMARY KEY,
    session_key            TEXT NOT NULL,
    source_scope           TEXT NOT NULL,
    client_request_id      TEXT NOT NULL,
    client_message_id      TEXT NOT NULL,
    request_fingerprint    TEXT NOT NULL,
    accepted_at            INTEGER NOT NULL,
    schema_version         INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1)
);
CREATE TABLE pending_chat_inputs (
    pending_input_id       TEXT PRIMARY KEY,
    session_key            TEXT NOT NULL,
    source_scope           TEXT NOT NULL,
    client_request_id      TEXT NOT NULL,
    client_message_id      TEXT NOT NULL,
    request_fingerprint    TEXT NOT NULL,
    payload_json           TEXT NOT NULL,
    position               INTEGER NOT NULL DEFAULT 0,
    state_revision         INTEGER NOT NULL DEFAULT 1 CHECK (state_revision >= 1),
    created_at             INTEGER NOT NULL,
    updated_at             INTEGER NOT NULL,
    schema_version         INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1)
);
CREATE TABLE plan_revisions (
    revision_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    parent_revision_id TEXT,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    source_session_key TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    source_epoch INTEGER NOT NULL DEFAULT 0 CHECK (source_epoch >= 0),
    source_turn_id TEXT,
    source_message_id TEXT,
    title TEXT NOT NULL,
    markdown TEXT NOT NULL,
    steps TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1)
);
CREATE TABLE plan_runs (
    run_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    session_id TEXT NOT NULL,
    session_epoch INTEGER NOT NULL DEFAULT 0 CHECK (session_epoch >= 0),
    plan_revision_id TEXT NOT NULL,
    supersedes_run_id TEXT,
    driver_kind TEXT NOT NULL DEFAULT 'manual'
        CHECK (driver_kind IN ('manual', 'goal')),
    driver_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (
            status IN (
                'queued', 'running', 'paused', 'blocked',
                'completed', 'cancelled', 'superseded'
            )
        ),
    step_states TEXT NOT NULL,
    current_step_id TEXT,
    state_revision INTEGER NOT NULL DEFAULT 0 CHECK (state_revision >= 0),
    active_task_id TEXT,
    pause_reason TEXT,
    terminal_reason TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1)
);
CREATE TABLE project_workspaces (
    workspace_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    path_key TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    position_at INTEGER NOT NULL,
    pinned_at INTEGER,
    removed_at INTEGER,
    trusted_at INTEGER
);
CREATE TABLE router_decisions (
    decision_id      TEXT PRIMARY KEY,
    session_key      TEXT NOT NULL,
    turn_index       INTEGER,
    ts_ms            INTEGER NOT NULL,
    classifier       TEXT,
    proposed_tier    TEXT,
    confidence       REAL,
    probs            TEXT,
    flags            TEXT,
    final_tier       TEXT,
    provider         TEXT,
    model            TEXT,
    thinking_level   TEXT,
    source           TEXT,
    trail            TEXT,
    baseline_model   TEXT,
    savings_pct      REAL,
    executed_kind    TEXT
                       CHECK(executed_kind IN ('single', 'ensemble')),
    ensemble_profile TEXT,
    fallback_hops    INTEGER NOT NULL DEFAULT 0
, requested_provider TEXT, requested_model TEXT, executed_provider TEXT, executed_model TEXT, fallback_reason TEXT);
CREATE TABLE sandbox_execution_preferences (
    client_id TEXT PRIMARY KEY,
    desired_mode TEXT NOT NULL CHECK (desired_mode IN ('safe', 'full')),
    updated_at INTEGER NOT NULL
);
CREATE TABLE sandbox_policy (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    policy_version INTEGER NOT NULL,
    policy_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE sandbox_tokens (
    public_id TEXT PRIMARY KEY,
    token_version INTEGER NOT NULL,
    name TEXT NOT NULL,
    secret_digest BLOB NOT NULL,
    roles_json TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    last_used_at INTEGER,
    last_peer TEXT,
    revoked_at INTEGER
);
CREATE TABLE session_goals (
    session_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    session_epoch INTEGER NOT NULL DEFAULT 0 CHECK (session_epoch >= 0),
    goal_id TEXT NOT NULL UNIQUE,
    objective TEXT NOT NULL CHECK (length(objective) BETWEEN 1 AND 4000),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'blocked', 'usage_limited', 'complete')),
    state_revision INTEGER NOT NULL DEFAULT 1 CHECK (state_revision >= 1),
    objective_revision INTEGER NOT NULL DEFAULT 1 CHECK (objective_revision >= 1),
    progress_revision INTEGER NOT NULL DEFAULT 0 CHECK (progress_revision >= 0),
    progress_json TEXT,
    continuation_seq INTEGER NOT NULL DEFAULT 0 CHECK (continuation_seq >= 0),
    active_task_id TEXT,
    terminal_task_id TEXT,
    turns_started INTEGER NOT NULL DEFAULT 0 CHECK (turns_started >= 0),
    turns_settled INTEGER NOT NULL DEFAULT 0 CHECK (turns_settled >= 0),
    window_turns_started INTEGER NOT NULL DEFAULT 0 CHECK (window_turns_started >= 0),
    active_time_ms INTEGER NOT NULL DEFAULT 0 CHECK (active_time_ms >= 0),
    window_active_time_ms INTEGER NOT NULL DEFAULT 0 CHECK (window_active_time_ms >= 0),
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    reasoning_tokens INTEGER NOT NULL DEFAULT 0 CHECK (reasoning_tokens >= 0),
    cache_read_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
    cache_write_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
    pause_reason TEXT,
    blocked_reason TEXT,
    terminal_reason TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    finished_at_ms INTEGER,
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1), source_user_message_id TEXT,
    FOREIGN KEY (session_key) REFERENCES sessions(session_key) ON DELETE CASCADE
);
CREATE TABLE sessions (
    session_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    started_at INTEGER,
    ended_at INTEGER,
    runtime_ms INTEGER,
    last_channel TEXT,
    last_to TEXT,
    last_account_id TEXT,
    last_thread_id TEXT,
    delivery_context TEXT,
    model TEXT,
    model_provider TEXT,
    provider_override TEXT,
    model_override TEXT,
    auth_profile_override TEXT,
    auth_profile_override_source TEXT,
    context_tokens INTEGER,
    model_routing_mode TEXT,
    model_routing_revision INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens_fresh INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
    total_cost_usd REAL NOT NULL DEFAULT 0.0,
    billed_cost_usd REAL NOT NULL DEFAULT 0.0,
    estimated_cost_component_usd REAL NOT NULL DEFAULT 0.0,
    cost_source TEXT NOT NULL DEFAULT 'none',
    missing_cost_entries INTEGER NOT NULL DEFAULT 0,
    cache_read INTEGER NOT NULL DEFAULT 0,
    cache_write INTEGER NOT NULL DEFAULT 0,
    compaction_count INTEGER NOT NULL DEFAULT 0,
    session_file TEXT,
    spawned_by TEXT,
    parent_session_key TEXT,
    forked_from_parent INTEGER NOT NULL DEFAULT 0,
    spawn_depth INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    chat_type TEXT NOT NULL DEFAULT 'unknown',
    thinking_level TEXT,
    fast_mode INTEGER NOT NULL DEFAULT 0,
    verbose_level TEXT,
    reasoning_level TEXT,
    send_policy TEXT NOT NULL DEFAULT 'allow',
    queue_mode TEXT NOT NULL DEFAULT 'steer',
    collaboration_mode TEXT NOT NULL DEFAULT 'default',
    collaboration_revision INTEGER NOT NULL DEFAULT 0,
    active_plan_revision_id TEXT,
    label TEXT,
    display_name TEXT,
    derived_title TEXT,
    channel TEXT,
    group_id TEXT,
    subject TEXT,
    origin TEXT,
    workspace_id TEXT,
    agent_id TEXT NOT NULL DEFAULT 'main',
    schema_version INTEGER NOT NULL DEFAULT 1,
    epoch INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE telemetry_daily_usage (
    day TEXT PRIMARY KEY,
    conversation_turns INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    uploaded_at INTEGER
);
CREATE TABLE transcript_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    message_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,
    tool_call_id TEXT,
    reasoning_content TEXT,
    turn_usage TEXT,
    turn_context TEXT,
    created_at INTEGER NOT NULL,
    token_count INTEGER,
    provenance_kind TEXT,
    provenance_origin_session_id TEXT,
    provenance_source_session_key TEXT,
    provenance_source_channel TEXT,
    provenance_source_tool TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE turn_errors (
    error_id      TEXT PRIMARY KEY,
    turn_id       TEXT,
    session_key   TEXT NOT NULL,
    session_id    TEXT,
    ts_ms         INTEGER NOT NULL,
    surface       TEXT,
    error_class   TEXT,
    message       TEXT,
    traceback     TEXT,
    provider      TEXT,
    model         TEXT,
    fallback_hops INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE turn_ingress_receipts (
    receipt_id             TEXT PRIMARY KEY,
    source_scope           TEXT NOT NULL,
    request_session_key    TEXT NOT NULL,
    client_request_id      TEXT NOT NULL,
    request_fingerprint    TEXT NOT NULL,
    accepted_session_key   TEXT NOT NULL,
    session_id             TEXT NOT NULL,
    message_id             TEXT NOT NULL,
    task_id                TEXT,
    accepted_at            INTEGER NOT NULL,
    schema_version         INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE usage_billing_receipt_state (
    singleton_id                INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    tracking_started_at_ms      INTEGER NOT NULL CHECK (tracking_started_at_ms >= 0),
    schema_version              INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1)
);
INSERT INTO "usage_billing_receipt_state" VALUES(1,1789063575460,1);
CREATE TABLE usage_event_items (
    event_id                    TEXT NOT NULL,
    ordinal                     INTEGER NOT NULL CHECK (ordinal >= 0),
    provider                    TEXT,
    model                       TEXT,
    input_tokens                INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens               INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    reasoning_tokens            INTEGER NOT NULL DEFAULT 0 CHECK (reasoning_tokens >= 0),
    cache_read_tokens           INTEGER NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
    cache_write_tokens          INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    total_tokens                INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
    cost_nanos                  INTEGER NOT NULL DEFAULT 0 CHECK (cost_nanos >= 0),
    billed_cost_nanos           INTEGER NOT NULL DEFAULT 0 CHECK (billed_cost_nanos >= 0),
    estimated_cost_nanos        INTEGER NOT NULL DEFAULT 0 CHECK (estimated_cost_nanos >= 0),
    cost_source                 TEXT NOT NULL DEFAULT 'none',
    estimate_basis              TEXT,
    price_source                TEXT,
    schema_version              INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (event_id, ordinal),
    FOREIGN KEY (event_id) REFERENCES usage_events(event_id) ON DELETE CASCADE,
    CHECK (cost_nanos = billed_cost_nanos + estimated_cost_nanos)
);
CREATE TABLE usage_events (
    event_id                    TEXT PRIMARY KEY,
    execution_id                TEXT NOT NULL,
    call_index                  INTEGER NOT NULL CHECK (call_index >= 0),
    turn_id                     TEXT,
    agent_run_id                TEXT,
    parent_turn_id              TEXT,
    session_id                  TEXT NOT NULL,
    session_epoch               INTEGER NOT NULL DEFAULT 0 CHECK (session_epoch >= 0),
    agent_id                    TEXT NOT NULL DEFAULT 'main',
    run_kind                    TEXT NOT NULL DEFAULT 'default',
    provider                    TEXT,
    model                       TEXT,
    started_at_ms               INTEGER NOT NULL CHECK (started_at_ms >= 0),
    completed_at_ms             INTEGER,
    status                      TEXT NOT NULL DEFAULT 'started'
                                CHECK (status IN ('started', 'finalized', 'unknown')),
    input_tokens                INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens               INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    reasoning_tokens            INTEGER NOT NULL DEFAULT 0 CHECK (reasoning_tokens >= 0),
    cache_read_tokens           INTEGER NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
    cache_write_tokens          INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    total_tokens                INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
    cost_nanos                  INTEGER NOT NULL DEFAULT 0 CHECK (cost_nanos >= 0),
    billed_cost_nanos           INTEGER NOT NULL DEFAULT 0 CHECK (billed_cost_nanos >= 0),
    estimated_cost_nanos        INTEGER NOT NULL DEFAULT 0 CHECK (estimated_cost_nanos >= 0),
    cost_source                 TEXT NOT NULL DEFAULT 'none',
    estimate_basis              TEXT,
    price_source                TEXT,
    coverage_status             TEXT NOT NULL DEFAULT 'pending',
    missing_cost_entries        INTEGER NOT NULL DEFAULT 0
                                CHECK (missing_cost_entries >= 0),
    unknown_reason              TEXT,
    origin                      TEXT NOT NULL,
    schema_version              INTEGER NOT NULL DEFAULT 1,
    UNIQUE (execution_id, call_index),
    CHECK (completed_at_ms IS NULL OR completed_at_ms >= started_at_ms),
    CHECK (cost_nanos = billed_cost_nanos + estimated_cost_nanos)
);
CREATE TABLE usage_item_billing_receipts (
    event_id                    TEXT NOT NULL,
    ordinal                     INTEGER NOT NULL CHECK (ordinal >= 0),
    currency                    TEXT NOT NULL
                                CHECK (length(currency) = 3 AND currency = upper(currency)),
    status                      TEXT NOT NULL
                                CHECK (status IN ('confirmed', 'pending')),
    amount_nanos                INTEGER CHECK (amount_nanos >= 0),
    usd_equivalent_nanos        INTEGER CHECK (usd_equivalent_nanos >= 0),
    fx_native_per_usd_nanos     INTEGER NOT NULL
                                CHECK (fx_native_per_usd_nanos > 0),
    schema_version              INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1),
    PRIMARY KEY (event_id, ordinal),
    FOREIGN KEY (event_id, ordinal)
        REFERENCES usage_event_items(event_id, ordinal) ON DELETE CASCADE,
    CHECK (
        (status = 'confirmed' AND amount_nanos IS NOT NULL
         AND usd_equivalent_nanos IS NOT NULL)
        OR
        (status = 'pending' AND usd_equivalent_nanos IS NULL)
    )
);
CREATE TABLE usage_ledger_state (
    singleton_id                INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    ledger_started_at_ms        INTEGER NOT NULL CHECK (ledger_started_at_ms >= 0),
    backfill_status             TEXT NOT NULL DEFAULT 'pending'
                                CHECK (backfill_status IN
                                       ('pending', 'running', 'complete',
                                        'partial', 'failed')),
    cursor_created_at_ms        INTEGER,
    cursor_session_id           TEXT,
    cursor_message_id           TEXT,
    backfilled_event_count      INTEGER NOT NULL DEFAULT 0
                                CHECK (backfilled_event_count >= 0),
    backfilled_cost_nanos       INTEGER NOT NULL DEFAULT 0
                                CHECK (backfilled_cost_nanos >= 0),
    anomaly_count               INTEGER NOT NULL DEFAULT 0 CHECK (anomaly_count >= 0),
    last_error_code             TEXT,
    updated_at_ms               INTEGER NOT NULL CHECK (updated_at_ms >= 0),
    schema_version              INTEGER NOT NULL DEFAULT 1,
    CHECK (
        (cursor_created_at_ms IS NULL AND cursor_session_id IS NULL
         AND cursor_message_id IS NULL)
        OR
        (cursor_created_at_ms IS NOT NULL AND cursor_session_id IS NOT NULL
         AND cursor_message_id IS NOT NULL)
    )
);
CREATE TABLE usage_legacy_baselines (
    session_id                  TEXT NOT NULL,
    session_epoch               INTEGER NOT NULL DEFAULT 0 CHECK (session_epoch >= 0),
    agent_id                    TEXT NOT NULL DEFAULT 'main',
    captured_at_ms              INTEGER NOT NULL CHECK (captured_at_ms >= 0),
    input_tokens                INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens               INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    total_tokens                INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
    cache_read_tokens           INTEGER NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
    cache_write_tokens          INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    cost_nanos                  INTEGER NOT NULL DEFAULT 0 CHECK (cost_nanos >= 0),
    billed_cost_nanos           INTEGER NOT NULL DEFAULT 0 CHECK (billed_cost_nanos >= 0),
    estimated_cost_nanos        INTEGER NOT NULL DEFAULT 0 CHECK (estimated_cost_nanos >= 0),
    cost_source                 TEXT NOT NULL DEFAULT 'none',
    missing_cost_entries        INTEGER NOT NULL DEFAULT 0
                                CHECK (missing_cost_entries >= 0),
    schema_version              INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (session_id, session_epoch),
    CHECK (cost_nanos = billed_cost_nanos + estimated_cost_nanos)
);
CREATE TABLE "yoyo_lock" ("locked" INT DEFAULT 1, "ctime" TIMESTAMP,"pid" INT NOT NULL,PRIMARY KEY ("locked"));
CREATE INDEX idx_agent_tasks_session_status ON agent_tasks(session_key, status);
CREATE INDEX idx_agent_tasks_status_updated ON agent_tasks(status, updated_at);
CREATE INDEX idx_meta_run_steps_status ON meta_skill_run_steps(status);
CREATE INDEX idx_meta_runs_name_started ON meta_skill_runs(meta_skill_name, started_at_ms DESC);
CREATE INDEX idx_meta_runs_status_started ON meta_skill_runs(status, started_at_ms DESC);
CREATE INDEX idx_meta_runs_session ON meta_skill_runs(session_key, started_at_ms DESC);
CREATE INDEX idx_meta_runs_started ON meta_skill_runs(started_at_ms DESC);
CREATE UNIQUE INDEX uq_one_awaiting_per_session ON meta_skill_runs(session_key) WHERE status = 'awaiting_user';
CREATE INDEX idx_router_decisions_session_ts ON router_decisions(session_key, ts_ms);
CREATE INDEX idx_router_decisions_ts ON router_decisions(ts_ms);
CREATE INDEX idx_turn_errors_session_ts ON turn_errors(session_key, ts_ms);
CREATE UNIQUE INDEX uq_turn_ingress_receipts_request ON turn_ingress_receipts(source_scope, request_session_key, client_request_id);
CREATE INDEX idx_turn_ingress_receipts_accepted_session ON turn_ingress_receipts(accepted_session_key, accepted_at);
CREATE INDEX idx_turn_errors_ts_error ON turn_errors(ts_ms, error_id);
CREATE INDEX idx_usage_events_completed ON usage_events(completed_at_ms, event_id);
CREATE INDEX idx_usage_events_session_completed ON usage_events(session_id, completed_at_ms, event_id);
CREATE INDEX idx_usage_events_agent_completed ON usage_events(agent_id, completed_at_ms, event_id);
CREATE INDEX idx_usage_events_status_completed ON usage_events(status, completed_at_ms, event_id);
CREATE INDEX idx_usage_events_status_started ON usage_events(status, started_at_ms, event_id);
CREATE INDEX idx_usage_event_items_model ON usage_event_items(model, event_id, ordinal);
CREATE INDEX idx_usage_event_items_provider ON usage_event_items(provider, event_id, ordinal);
CREATE INDEX idx_usage_legacy_baselines_captured ON usage_legacy_baselines(captured_at_ms, session_id);
CREATE UNIQUE INDEX idx_plan_revisions_plan_generation
    ON plan_revisions(plan_id, generation)
    ;
CREATE INDEX idx_plan_revisions_source_session
    ON plan_revisions(source_session_key, created_at)
    ;
CREATE UNIQUE INDEX idx_plan_revisions_source_message
    ON plan_revisions(source_session_id, source_message_id)
    WHERE source_message_id IS NOT NULL
    ;
CREATE TRIGGER plan_revisions_immutable
BEFORE UPDATE ON plan_revisions
BEGIN
    SELECT RAISE(ABORT, 'plan revisions are immutable');
END;
CREATE UNIQUE INDEX idx_plan_runs_active_session
    ON plan_runs(session_key)
    WHERE status IN ('queued', 'running', 'paused', 'blocked')
    ;
CREATE INDEX idx_plan_runs_session_history
    ON plan_runs(session_key, created_at)
    ;
CREATE INDEX idx_plan_runs_revision
    ON plan_runs(plan_revision_id, created_at)
    ;
CREATE INDEX idx_plan_runs_driver
    ON plan_runs(driver_id)
    WHERE driver_id IS NOT NULL
    ;
CREATE INDEX idx_project_workspaces_order ON project_workspaces(removed_at, pinned_at DESC, position_at DESC);
CREATE INDEX idx_sessions_workspace_id ON sessions(workspace_id);
CREATE INDEX idx_sandbox_tokens_active
ON sandbox_tokens(revoked_at, created_at)
;
CREATE UNIQUE INDEX uq_meta_control_intents_correlation ON meta_control_intents(session_key, control_kind, correlation_id);
CREATE INDEX idx_meta_control_intents_session_status ON meta_control_intents(session_key, status, created_at);
CREATE UNIQUE INDEX uq_meta_launch_drafts_request ON meta_launch_drafts(session_key, client_request_id);
CREATE INDEX idx_meta_launch_drafts_session_expiry ON meta_launch_drafts(session_key, expires_at, created_at);
CREATE INDEX idx_meta_launch_discard_tombstones_expiry ON meta_launch_discard_tombstones(expires_at, created_at);
CREATE UNIQUE INDEX idx_session_goals_active_task
    ON session_goals(active_task_id)
    WHERE active_task_id IS NOT NULL
    ;
CREATE INDEX idx_session_goals_status
    ON session_goals(status, updated_at_ms)
    ;
CREATE UNIQUE INDEX uq_goal_command_receipts_request
    ON goal_command_receipts(source_scope, request_session_key, client_request_id)
    ;
CREATE INDEX idx_goal_command_receipts_session
    ON goal_command_receipts(request_session_key, created_at_ms)
    ;
CREATE UNIQUE INDEX uq_pending_chat_inputs_request
ON pending_chat_inputs(session_key, client_request_id)
;
CREATE UNIQUE INDEX uq_pending_chat_inputs_message
ON pending_chat_inputs(session_key, client_message_id)
;
CREATE INDEX idx_pending_chat_inputs_session_order
ON pending_chat_inputs(session_key, position, created_at, pending_input_id)
;
CREATE INDEX idx_pending_chat_input_cancellations_session
ON pending_chat_input_cancellations(session_key, cancelled_at, pending_input_id)
;
CREATE UNIQUE INDEX uq_pending_chat_input_dispatch_request
ON pending_chat_input_dispatch_receipts(source_scope, session_key, client_request_id)
;
CREATE UNIQUE INDEX uq_pending_chat_input_dispatch_message
ON pending_chat_input_dispatch_receipts(session_key, client_message_id)
;
CREATE INDEX idx_pending_chat_input_dispatch_session
ON pending_chat_input_dispatch_receipts(session_key, accepted_at, pending_input_id)
;
CREATE INDEX idx_artifact_documents_session
    ON artifact_documents(session_key, updated_at DESC)
    ;
CREATE INDEX idx_artifact_revisions_document
    ON artifact_revisions(document_id, generation DESC)
    ;
CREATE INDEX idx_artifact_revisions_artifact
    ON artifact_revisions(artifact_id, document_id)
    ;
CREATE INDEX idx_artifact_change_sets_document_status
    ON artifact_change_sets(document_id, status, updated_at DESC)
    ;
CREATE UNIQUE INDEX idx_artifact_change_sets_turn
    ON artifact_change_sets(turn_id)
    WHERE turn_id IS NOT NULL
    ;
CREATE INDEX idx_artifact_anchors_revision
    ON artifact_anchors(document_id, revision_id)
    ;
CREATE INDEX idx_artifact_edit_sessions_document_status
    ON artifact_edit_sessions(document_id, status, updated_at DESC)
    ;
CREATE INDEX idx_artifact_edit_sessions_expiry
    ON artifact_edit_sessions(status, expires_at)
    ;
CREATE INDEX idx_artifact_writer_leases_expiry
    ON artifact_writer_leases(expires_at)
    ;
CREATE INDEX idx_artifact_audit_document_sequence
    ON artifact_audit_events(document_id, sequence)
    ;
CREATE TRIGGER artifact_revisions_immutable
    BEFORE UPDATE ON artifact_revisions
    BEGIN
        SELECT RAISE(ABORT, 'artifact revisions are immutable');
    END;
CREATE TRIGGER artifact_anchors_immutable
    BEFORE UPDATE ON artifact_anchors
    BEGIN
        SELECT RAISE(ABORT, 'artifact anchors are immutable');
    END;
CREATE TRIGGER artifact_audit_events_immutable
    BEFORE UPDATE ON artifact_audit_events
    BEGIN
        SELECT RAISE(ABORT, 'artifact audit events are immutable');
    END;
CREATE INDEX idx_artifact_prompt_annotations_session_status
    ON artifact_prompt_annotations(
        session_key, session_id, session_epoch, status, created_at, annotation_id
    )
    ;
CREATE INDEX idx_artifact_prompt_annotations_document_revision
    ON artifact_prompt_annotations(document_id, revision_id, status)
    ;
CREATE INDEX idx_artifact_mutation_attempts_document_status
    ON artifact_mutation_attempts(document_id, status, updated_at DESC)
    ;
CREATE UNIQUE INDEX idx_artifact_mutation_attempts_candidate
    ON artifact_mutation_attempts(candidate_session_id, candidate_artifact_id)
    WHERE candidate_artifact_id IS NOT NULL
    ;
CREATE INDEX idx_document_source_bindings_session
    ON document_source_bindings(session_id, source_type, source_resource_id)
    ;
CREATE INDEX idx_document_import_attempts_status
    ON document_import_attempts(session_id, status, updated_at)
    ;
CREATE INDEX idx_document_publications_session
    ON document_publications(session_id, created_at DESC, publication_id)
    ;
CREATE INDEX idx_document_publications_document
    ON document_publications(document_id, created_at DESC, publication_id)
    ;
CREATE INDEX idx_document_publish_attempts_status
    ON document_publish_attempts(session_id, status, updated_at)
    ;
CREATE TRIGGER document_source_bindings_immutable
    BEFORE UPDATE ON document_source_bindings
    BEGIN
        SELECT RAISE(ABORT, 'document source bindings are immutable');
    END;
CREATE TRIGGER document_publications_immutable
    BEFORE UPDATE ON document_publications
    BEGIN
        SELECT RAISE(ABORT, 'document publications are immutable');
    END;
DELETE FROM "sqlite_sequence";
COMMIT;
