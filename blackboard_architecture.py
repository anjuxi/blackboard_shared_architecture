import itertools;
import threading;
import time;
from concurrent.futures import ThreadPoolExecutor;
from dataclasses import dataclass, asdict;

# 常量定义
V_最大轮次 = 20;  # 总轮次上限，防止黑板永远停不下来。
V_收敛静默轮数 = 2;  # 连续多少轮没有新增记录就认为问题已经收敛。
V_置信度门槛 = 0.6;  # 低于该置信度的假设不参与冲突比较，避免把试探性猜测当成正式分歧。
V_冲突滞留轮数 = 2;  # 二次复核策略下，冲突滞留这么多轮仍无结论，就按最高置信度兜底。
V_默认策略 = "二次复核";  # 可选值：最高置信度、最新写入、二次复核。
V_最大并行度 = 4;  # 同一轮里同时运行的智能体数量上限。

# 数据结构定义
@dataclass(frozen=True)
class BoardItem:
    # 黑板上的条目，写完就不许改，这样整条证据链才能往回查。
    # 类型分五种：note 是原始资料，evidence 是证据，hypothesis 是假设，conflict 是冲突，conclusion 是结论。
    # references 存自己引用了哪些条目，tags 存标签，智能体靠标签判断新信息跟自己的专业有没有关系。
    item_id: str;
    author: str;
    kind: str;
    subject: str;
    value: str;
    confidence: float;
    round_no: int;
    created_at: float;
    references: tuple[str, ...] = ();
    tags: tuple[str, ...] = ();

    def to_dict(self):
        # 把条目转成字典，便于打印和导出。
        return asdict(self);


@dataclass(frozen=True)
class Conflict:
    # 一次观点冲突，记下同一个主体下互相打架的那几条假设。
    subject: str;
    claims: tuple[BoardItem, ...];

    @property
    def signature(self):
        # 签名由主体和断言值排序拼成，用来去重，同一场冲突不重复发。
        values = sorted({claim.value for claim in self.claims});
        return f"{self.subject}|{'|'.join(values)}";

    def describe(self):
        # 生成冲突描述，直接写回黑板，供复核类智能体阅读。
        parts = [f"{claim.author}认为“{claim.value}”（置信度 {claim.confidence:.2f}，条目 {claim.item_id}）" for claim in self.claims];
        return f"关于“{self.subject}”存在分歧：" + "；".join(parts);


# 黑板定义
class Blackboard:
    # 结构化共享黑板，所有智能体都只跟它打交道。
    # 内部用一个列表按写入顺序存条目，读写都加锁，几个智能体同时写也不会乱。

    def __init__(self):
        # 初始化黑板：编号从 B001 开始，版本号从 0 开始。
        self._items: list[BoardItem] = [];
        self._lock = threading.RLock();
        self._version = 0;
        self._counter = itertools.count(1);

    @property
    def version(self):
        # 黑板版本号，每次成功写入都会加一，可作为"有没有新信息"的判据。
        with self._lock:
            return self._version;

    def write(self, author, kind, subject, value, confidence=0.5, references=(), tags=(), round_no=0):
        # 往黑板追加一条记录。
        # author 是写入方，kind 是类型，subject 是断言主体，value 是内容，confidence 是置信度。
        # references 填引用的条目编号，tags 填标签，round_no 是轮次，初始资料统一记 0 轮。
        # 同一个写入方重复写相同内容时返回 None，省得几个智能体来回写同一句话。
        normalized = " ".join(str(value).split()).lower();
        confidence = max(0.0, min(1.0, float(confidence)));
        with self._lock:
            for item in self._items:
                if (item.author, item.kind, item.subject) != (author, kind, subject):
                    continue;
                if " ".join(item.value.split()).lower() == normalized:
                    return None;
            item = BoardItem(
                item_id=f"B{next(self._counter):03d}",
                author=author,
                kind=kind,
                subject=subject,
                value=str(value),
                confidence=confidence,
                round_no=round_no,
                created_at=time.time(),
                references=tuple(references),
                tags=tuple(tags),
            );
            self._items.append(item);
            self._version += 1;
            return item;

    def snapshot(self):
        # 取出当前全部条目的快照，读操作同样加锁，避免并发时读到半截数据。
        with self._lock:
            return tuple(self._items);

    def items(self, kind=None, subject=None, tag=None):
        # 按类型、主体或标签筛条目，智能体主要靠它看黑板上有哪些材料能用。
        # 三个参数传 None 就是不限，返回顺序跟写入顺序一致。
        with self._lock:
            result = [];
            for item in self._items:
                if kind is not None and item.kind != kind:
                    continue;
                if subject is not None and item.subject != subject:
                    continue;
                if tag is not None and tag not in item.tags:
                    continue;
                result.append(item);
            return tuple(result);

    def latest(self, subject, kind=None):
        # 取某个主体下最新的那条，常用来看结论出来没有。
        items = self.items(kind=kind, subject=subject);
        return items[-1] if items else None;

    def has(self, author=None, kind=None, subject=None):
        # 检查黑板上有没有符合条件的记录。
        # 智能体常拿它做幂等检查，写过的东西就不写第二遍。
        with self._lock:
            for item in self._items:
                if author is not None and item.author != author:
                    continue;
                if kind is not None and item.kind != kind:
                    continue;
                if subject is not None and item.subject != subject:
                    continue;
                return True;
            return False;

    def render(self):
        # 把黑板内容渲染成可读文本，按写入顺序展示，便于复盘整条证据链。
        lines = [];
        for item in self.snapshot():
            first_line = item.value.splitlines()[0];
            lines.append(f"[{item.item_id}] 第{item.round_no}轮 {item.author} / {item.kind} / {item.subject}：{first_line}（置信度 {item.confidence:.2f}）");
        return "\n".join(lines);


# 仲裁定义
class Arbiter:
    # 可选的仲裁模块，只管三件事：查冲突、去重、判断该不该停。
    # 它不派活，这正是黑板架构和星型架构的分水岭，星型那边是主管说了算。

    def __init__(self, strategy=V_默认策略):
        # strategy 支持三种：最高置信度、最新写入、二次复核。
        self.strategy = strategy;
        self._conflict_rounds: dict[str, int] = {};

    def detect_conflicts(self, board):
        # 找出还没解决的冲突。
        # 做法是按主体把假设归堆，同一个主体下出现两种以上说法就算打架。
        # 置信度低于门槛的假设不参与比较，免得把试探性的猜测也当成正式分歧。
        # 已经被更新结论压住的冲突不再报，返回剩下的。
        grouped: dict[str, list[BoardItem]] = {};
        for item in board.items(kind="hypothesis"):
            if item.confidence < V_置信度门槛:
                continue;
            grouped.setdefault(item.subject, []).append(item);
        conflicts = [];
        for subject, claims in grouped.items():
            if len({claim.value for claim in claims}) < 2:
                continue;
            if self._resolved(board, subject, claims):
                continue;
            conflicts.append(Conflict(subject=subject, claims=tuple(claims)));
        return conflicts;

    def _resolved(self, board, subject, claims):
        # 看冲突是不是已经被结论盖住了。
        # 同一轮里几个智能体是并发写的，时间戳谁先谁后说不准，所以这里按轮次比。
        conclusion = board.latest(subject, kind="conclusion");
        if conclusion is None:
            return False;
        newest_claim_round = max(claim.round_no for claim in claims);
        return conclusion.round_no >= newest_claim_round;

    def _pick(self, claims):
        # 按策略挑赢家。最新写入就比写入时间，其余策略比置信度。
        if self.strategy == "最新写入":
            return max(claims, key=lambda claim: (claim.created_at, claim.item_id));
        return max(claims, key=lambda claim: (claim.confidence, claim.created_at));

    def patrol(self, board, round_no):
        # 每轮巡检一遍黑板。
        # 遇到新冲突，要么按策略直接裁决，要么发一条复核请求，等复核智能体接手。
        # 二次复核策略下，冲突压了好几轮还没结论，就按最高置信度兜底，别让系统空转。
        # 返回这一轮仲裁器自己写进黑板的条目。
        written = [];
        for conflict in self.detect_conflicts(board):
            if conflict.signature not in self._conflict_rounds:
                self._conflict_rounds[conflict.signature] = round_no;
                if self.strategy in ("最高置信度", "最新写入"):
                    winner = self._pick(conflict.claims);
                    item = board.write(
                        author="仲裁器",
                        kind="conclusion",
                        subject=conflict.subject,
                        value=winner.value,
                        confidence=winner.confidence,
                        references=tuple(claim.item_id for claim in conflict.claims),
                        tags=("仲裁结论", self.strategy),
                        round_no=round_no,
                    );
                else:
                    item = board.write(
                        author="仲裁器",
                        kind="conflict",
                        subject=conflict.subject,
                        value=conflict.describe(),
                        confidence=max(claim.confidence for claim in conflict.claims),
                        references=tuple(claim.item_id for claim in conflict.claims),
                        tags=("复核",),
                        round_no=round_no,
                    );
                if item is not None:
                    written.append(item);
            elif self.strategy == "二次复核" and round_no - self._conflict_rounds[conflict.signature] >= V_冲突滞留轮数:
                # 复核智能体迟迟没有给出结论时，仲裁器按最高置信度兜底，避免无限循环。
                winner = self._pick(conflict.claims);
                item = board.write(
                    author="仲裁器",
                    kind="conclusion",
                    subject=conflict.subject,
                    value=winner.value,
                    confidence=winner.confidence,
                    references=tuple(claim.item_id for claim in conflict.claims),
                    tags=("仲裁兜底",),
                    round_no=round_no,
                );
                if item is not None:
                    written.append(item);
        return written;

    def should_stop(self, board, round_no, quiet_rounds):
        # 判断能不能停。
        # 轮次到顶直接停，这是兜底；正常情况要求黑板连续几轮没新增，且没有悬着的冲突。
        # 返回二元组：是否停止，以及原因。
        if round_no >= V_最大轮次:
            return True, "达到最大轮次上限";
        open_conflicts = self.detect_conflicts(board);
        if quiet_rounds >= V_收敛静默轮数 and not open_conflicts:
            return True, "黑板连续静默且冲突已消解";
        return False, "";


# 智能体定义
class ExpertAgent:
    # 智能体基类，只规定两件事：什么时候参与，参与后写什么。
    # 子类不用知道别的智能体叫什么，也不用操心谁会读自己的输出。

    name = "专家智能体";

    def should_activate(self, board):
        # 状态触发条件：默认不参与，子类根据黑板上的信息自行覆盖。
        return False;

    def act(self, board):
        # 读黑板，返回这一轮想写进去的条目描述。
        # 每条描述是个字典，字段跟 write 的参数对应，author 由运行时统一填。
        # 返回空列表就表示这一轮没什么可补的。
        return [];


class ImagingAgent(ExpertAgent):
    # 影像智能体：看到影像资料后写一条影像所见，顺手给个病因判断。
    name = "影像智能体";

    def should_activate(self, board):
        # 必须有影像资料，而且自己还没写过影像结论，两个条件缺一不可。
        return board.has(subject="影像检查") and not board.has(author=self.name, subject="影像所见");

    def act(self, board):
        # 读取原始影像描述，写一条证据和一条病因假设，两者都引用原始资料。
        source = board.latest("影像检查");
        return [
            {
                "kind": "evidence",
                "subject": "影像所见",
                "value": "阑尾增粗约 9mm，周围脂肪间隙模糊并见少量渗出，符合急性阑尾炎表现",
                "confidence": 0.88,
                "references": (source.item_id,),
                "tags": ("影像",),
            },
            {
                "kind": "hypothesis",
                "subject": "病因",
                "value": "急性阑尾炎",
                "confidence": 0.86,
                "references": (source.item_id,),
                "tags": ("影像",),
            },
        ];


class LabAgent(ExpertAgent):
    # 检验智能体：看到化验单后补实验室证据，也给出自己的初步判断。
    name = "检验智能体";

    def should_activate(self, board):
        # 黑板出现检验报告，且自己还没写过检验结论时参与。
        return board.has(subject="检验报告") and not board.has(author=self.name, subject="检验所见");

    def act(self, board):
        # 检验数据提示细菌感染，但单看指标无法区分阑尾炎和肠胃炎，所以置信度刻意给得低一些。
        source = board.latest("检验报告");
        return [
            {
                "kind": "evidence",
                "subject": "检验所见",
                "value": "白细胞 15.2×10^9/L、中性粒细胞 88%、C 反应蛋白 46mg/L，提示细菌性感染",
                "confidence": 0.80,
                "references": (source.item_id,),
                "tags": ("检验",),
            },
            {
                "kind": "hypothesis",
                "subject": "病因",
                "value": "急性肠胃炎",
                "confidence": 0.66,
                "references": (source.item_id,),
                "tags": ("检验",),
            },
        ];


class ClinicalAgent(ExpertAgent):
    # 临床评估智能体：等影像和检验的证据都齐了，再结合体征说话。
    name = "临床评估智能体";

    def should_activate(self, board):
        # 必须同时看到影像所见和检验所见才动，这就是状态触发。
        return (
            board.has(subject="影像所见")
            and board.has(subject="检验所见")
            and not board.has(author=self.name, subject="体征")
        );

    def act(self, board):
        # 结合查体结果给出临床判断，引用前序两条证据，方便后续追溯。
        imaging = board.latest("影像所见");
        lab = board.latest("检验所见");
        return [
            {
                "kind": "evidence",
                "subject": "体征",
                "value": "麦氏点压痛、反跳痛阳性，体温 38.5℃，符合急性阑尾炎典型体征",
                "confidence": 0.85,
                "references": (imaging.item_id, lab.item_id),
                "tags": ("临床",),
            },
            {
                "kind": "hypothesis",
                "subject": "病因",
                "value": "急性阑尾炎",
                "confidence": 0.90,
                "references": (imaging.item_id, lab.item_id),
                "tags": ("临床",),
            },
        ];


class ReviewAgent(ExpertAgent):
    # 复核智能体：只接标签为"复核"的冲突请求，看完所有证据再给结论。
    name = "复核智能体";

    def should_activate(self, board):
        # 有复核请求，并且该主体还没有结论时参与。
        for conflict in board.items(kind="conflict", tag="复核"):
            if board.latest(conflict.subject, kind="conclusion") is None:
                return True;
        return False;

    def act(self, board):
        # 找到第一个未裁决的冲突，汇总候选观点和全部证据，选支持度最高的方案。
        for conflict in board.items(kind="conflict", tag="复核"):
            if board.latest(conflict.subject, kind="conclusion") is not None:
                continue;
            candidates = board.items(kind="hypothesis", subject=conflict.subject);
            if not candidates:
                continue;
            evidence = board.items(kind="evidence");
            supported = [];
            for candidate in candidates:
                score = candidate.confidence;
                # 证据文本里直接出现该诊断结论，就给候选加一点权重，模拟专家交叉印证。
                for item in evidence:
                    if candidate.value in item.value:
                        score += 0.02;
                supported.append((score, candidate));
            best_score, best = max(supported, key=lambda pair: (pair[0], pair[1].created_at));
            references = tuple(dict.fromkeys([claim.item_id for claim in candidates] + [item.item_id for item in evidence]));
            return [
                {
                    "kind": "conclusion",
                    "subject": conflict.subject,
                    "value": best.value,
                    "confidence": round(min(0.99, best_score + 0.04), 2),
                    "references": references,
                    "tags": ("复核结论",),
                },
            ];
        return [];


class SummaryAgent(ExpertAgent):
    # 汇总智能体：病因有结论后，把整块黑板压成一份能直接看的报告。
    name = "汇总智能体";

    def should_activate(self, board):
        # 有病因结论且还没有最终报告时参与。
        return board.has(kind="conclusion", subject="病因") and not board.has(subject="最终报告");

    def act(self, board):
        # 按"结论、证据、分歧"三层整理报告，并注明可用条目数，方便人工复核。
        conclusion = board.latest("病因", kind="conclusion");
        evidence = board.items(kind="evidence");
        hypotheses = board.items(kind="hypothesis");
        lines = [f"综合诊断：{conclusion.value}（置信度 {conclusion.confidence:.2f}）"];
        lines.append("依据：" + "；".join(f"{item.subject}——{item.value}" for item in evidence));
        if hypotheses:
            lines.append("分歧记录：" + "；".join(f"{item.author}提出{item.value}" for item in hypotheses));
        lines.append(f"黑板共沉淀 {len(board.snapshot())} 条记录，可逐条回溯。");
        return [
            {
                "kind": "conclusion",
                "subject": "最终报告",
                "value": "\n".join(lines),
                "confidence": conclusion.confidence,
                "references": tuple(item.item_id for item in board.snapshot()),
                "tags": ("报告",),
            },
        ];


# 运行时定义
class BlackboardRuntime:
    # 运行时负责循环：挑出这一轮能参与的智能体，并发跑完，再让仲裁器巡检。
    # 它不给智能体派活，参不参与完全看智能体自己的触发条件。

    def __init__(self, board, agents, arbiter=None, parallel=V_最大并行度):
        # board 是共享黑板，agents 是注册的专家列表，arbiter 是可选的仲裁模块。
        self.board = board;
        self.agents = list(agents);
        self.arbiter = arbiter if arbiter is not None else Arbiter();
        self.parallel = max(1, int(parallel));
        self.trace: list[str] = [];
        self.stop_reason = "";
        self.rounds = 0;

    def seed(self, facts):
        # 把初始资料写进黑板，相当于专家入场前已经摆在桌上的东西。
        # facts 是字典列表，字段跟 write 的参数对应，轮次统一记 0。
        # 返回实际写进去的条目。
        written = [];
        for fact in facts:
            item = self.board.write(round_no=0, **fact);
            if item is not None:
                written.append(item);
        return written;

    def run(self):
        # 主循环。
        # 每轮先让满足条件的智能体并发产出，再让仲裁器查冲突，最后判断要不要停。
        # 返回跑完之后的黑板。
        quiet_rounds = 0;
        for round_no in range(1, V_最大轮次 + 1):
            self.rounds = round_no;
            before = self.board.version;
            active = [agent for agent in self.agents if agent.should_activate(self.board)];
            produced = self._run_agents(active, round_no);
            produced.extend(self.arbiter.patrol(self.board, round_no));
            if self.board.version > before:
                quiet_rounds = 0;
            else:
                quiet_rounds += 1;
            self._log(round_no, active, produced);
            stop, reason = self.arbiter.should_stop(self.board, round_no, quiet_rounds);
            if stop:
                self.stop_reason = reason;
                break;
        else:
            self.stop_reason = "达到最大轮次上限";
        return self.board;

    def _run_agents(self, agents, round_no):
        # 并发跑这一轮的智能体。谁先谁后不影响结果，观点打架有仲裁器收拾。
        if not agents:
            return [];
        produced = [];
        if len(agents) == 1 or self.parallel == 1:
            for agent in agents:
                produced.extend(self._run_one(agent, round_no));
            return produced;
        with ThreadPoolExecutor(max_workers=min(self.parallel, len(agents))) as pool:
            futures = [pool.submit(self._run_one, agent, round_no) for agent in agents];
            for future in futures:
                produced.extend(future.result());
        return produced;

    def _run_one(self, agent, round_no):
        # 跑单个智能体，把它想写的东西逐条落到黑板上。
        written = [];
        try:
            specs = agent.act(self.board);
        except Exception as error:
            # 单个智能体出错不应该拖垮整块黑板，记录后继续让其他专家工作。
            self.trace.append(f"智能体 {agent.name} 执行失败：{error}");
            print(f"智能体 {agent.name} 执行失败：{error}！");
            return written;
        for spec in specs:
            item = self.board.write(author=agent.name, round_no=round_no, **spec);
            if item is not None:
                written.append(item);
        return written;

    def _log(self, round_no, active, produced):
        # 本轮情况记进审计日志，顺手打一行状态，方便观察收敛过程。
        if active:
            names = "、".join(agent.name for agent in active);
            self.trace.append(f"第{round_no}轮：参与智能体 {names}，新增记录 {len(produced)} 条");
            print(f"第 {round_no} 轮：{names}参与，新增 {len(produced)} 条记录！");
        else:
            self.trace.append(f"第{round_no}轮：无人参与，新增记录 0 条");
            print(f"第 {round_no} 轮：没人参与，黑板没有变化！");
        for item in produced:
            first_line = item.value.splitlines()[0];
            print(f"  {item.author} → [{item.item_id}] {item.kind} / {item.subject}：{first_line}（{item.confidence:.2f}）！");


# 演示场景
def build_demo(arbiter=None):
    # 组装多学科会诊场景。
    # arbiter 不传就用默认的二次复核策略。
    # 返回黑板和已经写好初始资料的运行时。
    board = Blackboard();
    agents = [ImagingAgent(), LabAgent(), ClinicalAgent(), ReviewAgent(), SummaryAgent()];
    runtime = BlackboardRuntime(board, agents, arbiter if arbiter is not None else Arbiter());
    facts = [
        {
            "author": "接诊智能体",
            "kind": "note",
            "subject": "主诉",
            "value": "转移性右下腹痛 18 小时，伴发热 38.5℃、恶心",
            "confidence": 0.95,
            "tags": ("病史",),
        },
        {
            "author": "接诊智能体",
            "kind": "note",
            "subject": "影像检查",
            "value": "腹部超声：右下腹阑尾区见增粗管状结构，直径约 9mm，周围少量渗出",
            "confidence": 0.90,
            "tags": ("影像检查",),
        },
        {
            "author": "接诊智能体",
            "kind": "note",
            "subject": "检验报告",
            "value": "血常规白细胞 15.2×10^9/L，中性粒细胞 88%，C 反应蛋白 46mg/L",
            "confidence": 0.90,
            "tags": ("检验报告",),
        },
    ];
    runtime.seed(facts);
    return board, runtime;


def main():
    # 跑一遍会诊，把每轮状态、完整黑板和最终报告打出来。
    print("——" * 50);
    print("【黑板共享架构演示：多学科会诊】");
    print("——" * 50);
    board, runtime = build_demo();
    start = time.time();
    runtime.run();
    elapsed = time.time() - start;
    print("——" * 50);
    print("【黑板完整记录】");
    print(board.render());
    print("——" * 50);
    print("【最终报告】");
    report = board.latest("最终报告");
    print(report.value if report is not None else "没有生成报告！");
    print("——" * 50);
    print(f"终止原因：{runtime.stop_reason}；共 {runtime.rounds} 轮，耗时 {elapsed:.3f} 秒！");
    print(f"✅ 演示完成，黑板共 {board.version} 条记录，全部可追溯！");


if __name__ == "__main__":
    main();
