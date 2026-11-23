# 运行方式：python -m unittest -v test_blackboard_architecture.py

import contextlib;
import io;
import threading;
import time;
import unittest;

from blackboard_architecture import (
    Blackboard,
    Arbiter,
    ImagingAgent,
    ReviewAgent,
    V_最大轮次,
    build_demo,
);

# 测试用例
class BlackboardTest(unittest.TestCase):
    # 黑板本身的行为：追加、去重、并发写入。

    def test_append_and_dedup(self):
        # 相同写入方重复写同一内容时，只保留第一条，版本号也只加一次。
        board = Blackboard();
        first = board.write(author="甲", kind="evidence", subject="证据", value="白细胞升高");
        second = board.write(author="甲", kind="evidence", subject="证据", value="  白细胞升高  ");
        self.assertIsNotNone(first);
        self.assertIsNone(second);
        self.assertEqual(board.version, 1);
        self.assertEqual(first.item_id, "B001");

    def test_concurrent_writes(self):
        # 八个线程各写二十条不同记录，黑板不能丢数据，版本号要等于写入总数。
        board = Blackboard();

        def worker(index):
            # 每个线程写入自己负责的二十条证据。
            for j in range(20):
                board.write(
                    author=f"智能体{index}",
                    kind="evidence",
                    subject=f"主题{index}-{j}",
                    value=f"内容{index}-{j}",
                );

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)];
        for thread in threads:
            thread.start();
        for thread in threads:
            thread.join();
        self.assertEqual(board.version, 160);
        self.assertEqual(len(board.snapshot()), 160);


class AgentTest(unittest.TestCase):
    # 智能体该不该参与，只看黑板状态。

    def test_trigger_by_state(self):
        # 没有影像资料时不参与，写入资料后参与一次，产出后再检查应变为不参与。
        board = Blackboard();
        agent = ImagingAgent();
        self.assertFalse(agent.should_activate(board));
        board.write(author="接诊智能体", kind="note", subject="影像检查", value="阑尾增粗");
        self.assertTrue(agent.should_activate(board));
        for spec in agent.act(board):
            board.write(author=agent.name, **spec);
        self.assertFalse(agent.should_activate(board));


class ArbiterTest(unittest.TestCase):
    # 三种裁决策略和终止条件。

    def test_review_policy(self):
        # 二次复核策略先发布冲突请求，复核智能体读取后给出结论，冲突随之消解。
        board = Blackboard();
        board.write(author="影像智能体", kind="hypothesis", subject="病因", value="急性阑尾炎", confidence=0.86, round_no=1);
        board.write(author="检验智能体", kind="hypothesis", subject="病因", value="急性肠胃炎", confidence=0.66, round_no=1);
        arbiter = Arbiter("二次复核");
        written = arbiter.patrol(board, 1);
        self.assertEqual(len(written), 1);
        self.assertEqual(written[0].kind, "conflict");
        reviewer = ReviewAgent();
        self.assertTrue(reviewer.should_activate(board));
        for spec in reviewer.act(board):
            board.write(author=reviewer.name, **spec, round_no=2);
        conclusion = board.latest("病因", kind="conclusion");
        self.assertIsNotNone(conclusion);
        self.assertEqual(conclusion.value, "急性阑尾炎");
        self.assertEqual(arbiter.detect_conflicts(board), []);

    def test_direct_policies(self):
        # 最高置信度和最新写入两种策略应分别选中不同的赢家。
        cases = [("最高置信度", "高置信方案"), ("最新写入", "后写方案")];
        for strategy, expected in cases:
            board = Blackboard();
            board.write(author="甲", kind="hypothesis", subject="方案", value="高置信方案", confidence=0.90, round_no=1);
            time.sleep(0.002);
            board.write(author="乙", kind="hypothesis", subject="方案", value="后写方案", confidence=0.70, round_no=1);
            Arbiter(strategy).patrol(board, 1);
            conclusion = board.latest("方案", kind="conclusion");
            self.assertIsNotNone(conclusion);
            self.assertEqual(conclusion.value, expected);

    def test_termination(self):
        # 静默轮数不足时不停止，达到阈值且无冲突才收敛，轮次到顶则强制停止。
        board = Blackboard();
        arbiter = Arbiter();
        self.assertEqual(arbiter.should_stop(board, 3, 1), (False, ""));
        stop, reason = arbiter.should_stop(board, 3, 2);
        self.assertTrue(stop);
        self.assertIn("静默", reason);
        stop, reason = arbiter.should_stop(board, V_最大轮次, 0);
        self.assertTrue(stop);
        self.assertIn("最大轮次", reason);


class DemoTest(unittest.TestCase):
    # 端到端跑一遍会诊。

    def test_demo_converges(self):
        # 端到端跑一遍，要求生成报告、结论正确、并在轮次上限内停止。
        board, runtime = build_demo();
        with contextlib.redirect_stdout(io.StringIO()):
            runtime.run();
        report = board.latest("最终报告");
        self.assertIsNotNone(report);
        self.assertIn("急性阑尾炎", report.value);
        conclusion = board.latest("病因", kind="conclusion");
        self.assertIsNotNone(conclusion);
        self.assertEqual(conclusion.value, "急性阑尾炎");
        self.assertLessEqual(runtime.rounds, V_最大轮次);
        self.assertTrue(runtime.stop_reason);


if __name__ == "__main__":
    unittest.main();
