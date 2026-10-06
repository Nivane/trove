"""trove validate —— 服务层测试:每个检查族一好一坏(零 LLM / 零网络)。

Fixture 全是 tmp 项目树(tmp/.trove/kb/<ds>、tmp/.trove/skills/...),不读
仓库 KB。api_key / 网络在这一层根本不参与:除 ``--live`` 外没有任何连接。
"""

from __future__ import annotations

from pathlib import Path

from trove.services.validate import run_validate
from trove.services.validate import service as validate_service
from trove.services.validate.service import SKILL_NODES, skill_node_call_sites

# 最小可编译语义模型(离线用 sqlite 档编译,表达式走 ANSI_SQL 兜底)。
_SEMANTICS = {
    "semantic_model": [{
        "name": "mini",
        "datasets": [{
            "name": "loan",
            "source": "loan",
            "primary_key": ["loan_id"],
            "fields": [
                {"name": "loan_id", "datatype": "Integer",
                 "description": "Loan id.",
                 "expression": {"dialects": [
                     {"dialect": "ANSI_SQL", "expression": "loan_id"}]}},
                {"name": "amount", "datatype": "Decimal",
                 "description": "Loan amount.",
                 "expression": {"dialects": [
                     {"dialect": "ANSI_SQL", "expression": "amount"}]}},
                {"name": "date", "datatype": "Date",
                 "description": "Booking date.",
                 "expression": {"dialects": [
                     {"dialect": "ANSI_SQL", "expression": "date"}]}},
            ]},
        # 第二个数据集**没有时间字段** —— window 求值失败的反例靠它构造。
        {"name": "branch",
         "source": "branch",
         "primary_key": ["branch_id"],
         "fields": [
             {"name": "branch_id", "datatype": "Integer",
              "description": "Branch id.",
              "expression": {"dialects": [
                  {"dialect": "ANSI_SQL", "expression": "branch_id"}]}},
             {"name": "name", "datatype": "String",
              "description": "Branch name.",
              "expression": {"dialects": [
                  {"dialect": "ANSI_SQL", "expression": "name"}]}},
         ]},
        ],
        "metrics": [{
            "name": "total_amount",
            "agg_time_dimension": "date",
            "expression": {"dialects": [
                {"dialect": "ANSI_SQL", "expression": "SUM(loan.amount)"}]},
        }, {
            "name": "branch_count",
            "expression": {"dialects": [
                {"dialect": "ANSI_SQL", "expression": "COUNT(branch.branch_id)"}]},
        }],
    }],
}

_SCHEMA_NOTES = {
    "tables": [{
        "name": "loan",
        "description": "Loans.",
        "columns": [
            {"name": "loan_id", "description": "Loan id."},
            {"name": "amount", "description": "Loan amount."},
            {"name": "date", "description": "Booking date."},
        ],
    }, {
        "name": "branch",
        "description": "Branches.",
        "columns": [
            {"name": "branch_id", "description": "Branch id."},
            {"name": "name", "description": "Branch name."},
        ],
    }],
}

_RULE = {
    "version": 1,
    "rules": [{
        "id": "loan-high",
        "name": "loan total above baseline",
        "severity": "warning",
        "subject": {"metrics": ["total_amount"]},
        "baseline": {"kind": "literal", "value": 100000.0},
        "conditions": ["delta > 0"],
    }],
}


def _write_yaml(path: Path, data: dict) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")


def _make_kb(tmp_path: Path, datasource: str = "mini", **files) -> Path:
    """tmp 项目树 + 一个 KB 目录;``files`` 按名覆盖(``None`` = 不写)。"""
    ds_dir = tmp_path / ".trove" / "kb" / datasource
    ds_dir.mkdir(parents=True, exist_ok=True)
    _write_yaml(ds_dir / "semantics.yml", _SEMANTICS)
    _write_yaml(ds_dir / "schema_notes.yml", _SCHEMA_NOTES)
    for name, data in files.items():
        if data is None:
            (ds_dir / f"{name}.yml").unlink(missing_ok=True)
        else:
            _write_yaml(ds_dir / f"{name}.yml", data)
    return ds_dir


def _make_skill(tmp_path: Path, dir_name: str, frontmatter: dict,
                body: str = "1. Read the caliber.\n2. Apply it.") -> Path:
    import yaml

    skills = tmp_path / ".trove" / "skills" / dir_name
    skills.mkdir(parents=True, exist_ok=True)
    fm = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
    (skills / "SKILL.md").write_text(f"---\n{fm}\n---\n\n{body}\n",
                                     encoding="utf-8")
    return skills


def _checks(report, prefix: str) -> list:
    return [i for i in report.issues if i.check.startswith(prefix)]


# ── happy path ───────────────────────────────────────────


async def test_clean_project_passes(tmp_path):
    """干净项目:KB + 一条可编译规则 + 一份已确认 required 技能 → 0 硬错误。"""
    _make_kb(tmp_path, decisions=_RULE)
    _make_skill(tmp_path, "loan-caliber", {
        "name": "loan-caliber", "description": "How to read loan amounts.",
        "triggers": {"node": "query_sketch"}, "tier": "required",
        "status": "confirmed",
    })
    report = await run_validate("mini", project_root=tmp_path)

    assert report.errors == [], [i.message for i in report.errors]
    assert report.ok and report.exit_code() == 0
    assert report.datasources == ["mini"]
    assert report.counts["mini.tables"] == 2
    assert report.counts["mini.rules"] == 1
    assert report.counts["org_skills"] == 1

    skill = next(m for m in report.mounts if m.kind == "skill")
    assert skill.mounts == ["query_sketch 节点系统提示(整篇注入)"]
    rule = next(m for m in report.mounts if m.kind == "rule")
    assert "调度面" in rule.mounts[0]


async def test_all_datasources_when_omitted(tmp_path):
    """不指定 --datasource:遍历 .trove/kb 下每个有 YAML 的目录。"""
    _make_kb(tmp_path, "mini")
    _make_kb(tmp_path, "other")
    report = await run_validate(project_root=tmp_path)
    assert report.datasources == ["mini", "other"]
    assert report.ok


async def test_missing_datasource_is_hard_error(tmp_path):
    _make_kb(tmp_path, "mini")
    report = await run_validate("ghost", project_root=tmp_path)
    assert report.errors and "KB 目录不存在" in report.errors[0].message
    assert report.exit_code() == 1


# ── KB ───────────────────────────────────────────────────


async def test_kb_bad_example_sql_is_error(tmp_path):
    """示例 SQL 引用不存在的表 → 硬错误(复用 lint_examples)。"""
    _make_kb(tmp_path, examples={"examples": [{
        "question": "How many loans?", "sql": "SELECT COUNT(*) FROM ghost"}]})
    report = await run_validate("mini", project_root=tmp_path)
    assert any("ghost" in i.message for i in _checks(report, "kb"))
    assert report.exit_code() == 1


async def test_kb_missing_semantics_is_hard_error(tmp_path):
    """无语义模型 = 查询管线整体拒绝该数据源 —— 干跑必须说出来。"""
    _make_kb(tmp_path, semantics=None)
    report = await run_validate("mini", project_root=tmp_path)
    assert any("semantics.yml" in i.message for i in _checks(report, "kb")
               if i.severity == "error")
    assert report.exit_code() == 1


async def test_kb_duplicate_examples_warn_only(tmp_path):
    """同题多例是 warning:默认放过,--strict 下才拦。"""
    _make_kb(tmp_path, examples={"examples": [
        {"question": "How many loans?", "sql": "SELECT COUNT(*) FROM loan"},
        {"question": "How many loans?", "sql": "SELECT COUNT(loan_id) FROM loan"},
    ]})
    report = await run_validate("mini", project_root=tmp_path)
    assert report.errors == []
    assert any(i.check == "kb.examples" and "同一问题" in i.message
               for i in report.warnings)
    assert report.exit_code() == 0
    assert report.exit_code(strict=True) == 1


async def test_kb_misplaced_masking_is_error(tmp_path):
    """顶层错放的 masking 会被解析器静默忽略 → 文档级 lint 报硬错误。"""
    data = dict(_SEMANTICS)
    data["masking"] = {"salt_ref": "MAIL_SALT"}
    _make_kb(tmp_path, semantics=data)
    report = await run_validate("mini", project_root=tmp_path)
    assert any("masking" in i.message for i in _checks(report, "kb")
               if i.severity == "error")


# ── decision rules ───────────────────────────────────────


async def test_rule_compiles_against_semantic_model(tmp_path):
    """编译门是真门:subject 里的 metric 在语义模型里存在 → 通过。"""
    _make_kb(tmp_path, decisions=_RULE)
    report = await run_validate("mini", project_root=tmp_path)
    assert _checks(report, "decision.compile") == []


async def test_rule_unknown_metric_is_error(tmp_path):
    """metric 不在语义模型里 = 到点必炸的调度 → 硬错误(运行时同一条 _compile)。"""
    bad = {"version": 1, "rules": [{
        "id": "ghost-metric", "severity": "warning",
        "subject": {"metrics": ["no_such_metric"]},
        "baseline": {"kind": "literal", "value": 1.0},
        "conditions": ["delta > 0"],
    }]}
    _make_kb(tmp_path, decisions=bad)
    report = await run_validate("mini", project_root=tmp_path)
    compile_errors = _checks(report, "decision.compile")
    assert compile_errors and compile_errors[0].severity == "error"
    assert compile_errors[0].target == "ghost-metric"
    assert report.exit_code() == 1


async def test_rule_window_compiles_when_time_field_resolves(tmp_path):
    """window 走运行期同一条 _resolve_window + 时间过滤注入 → 通过。"""
    win = {"version": 1, "rules": [{
        "id": "windowed-ok", "severity": "warning",
        "window": "本月",
        "subject": {"metrics": ["total_amount"]},
        "baseline": {"kind": "literal", "value": 1.0},
        "conditions": ["delta > 0"],
    }]}
    _make_kb(tmp_path, decisions=win)
    report = await run_validate("mini", project_root=tmp_path)
    assert report.errors == []


async def test_rule_window_without_time_field_is_error(tmp_path):
    """window 存在但溯源不到时间字段 → 硬错误(而不是静默不触发)。"""
    bad = {"version": 1, "rules": [{
        "id": "windowed-bad", "severity": "warning",
        "window": "本月",
        "subject": {"metrics": ["branch_count"]},
        "baseline": {"kind": "literal", "value": 1.0},
        "conditions": ["delta > 0"],
    }]}
    _make_kb(tmp_path, decisions=bad)
    report = await run_validate("mini", project_root=tmp_path)
    hits = _checks(report, "decision.compile")
    assert hits and "time field" in hits[0].message


async def test_disabled_rule_compile_failure_warns_only(tmp_path):
    """停用中的坏规则是草稿态:提示,不拦 CI。"""
    bad = {"version": 1, "rules": [{
        "id": "draft", "enabled": False, "severity": "warning",
        "subject": {"metrics": ["no_such_metric"]},
        "baseline": {"kind": "literal", "value": 1.0},
        "conditions": ["delta > 0"],
    }]}
    _make_kb(tmp_path, decisions=bad)
    report = await run_validate("mini", project_root=tmp_path)
    hits = _checks(report, "decision.compile")
    assert hits and hits[0].severity == "warning"
    rule = next(m for m in report.mounts if m.name == "draft")
    assert rule.status == "disabled"
    assert any("已停用" in n for n in rule.notes)


async def test_rule_bad_schema_is_error(tmp_path):
    """结构错误(重复 id)由 parse_document 抛 → 一条硬错误,不炸整轮。"""
    dup = {"version": 1, "rules": [
        {"id": "same", "severity": "warning",
         "subject": {"metrics": ["total_amount"]},
         "baseline": {"kind": "literal", "value": 1.0},
         "conditions": ["delta > 0"]},
        {"id": "same", "severity": "warning",
         "subject": {"metrics": ["total_amount"]},
         "baseline": {"kind": "literal", "value": 2.0},
         "conditions": ["delta > 0"]},
    ]}
    _make_kb(tmp_path, decisions=dup)
    report = await run_validate("mini", project_root=tmp_path)
    assert any("same" in i.message for i in _checks(report, "decision.schema"))
    assert report.exit_code() == 1


async def test_rule_dangling_action_template_is_error(tmp_path):
    """action.template 指向未声明的模板 = 触发时渲染不出来。"""
    bad = {"version": 1, "rules": [{
        "id": "acts", "severity": "warning",
        "subject": {"metrics": ["total_amount"]},
        "baseline": {"kind": "literal", "value": 1.0},
        "conditions": ["delta > 0"],
        "action": {"template": "ghost-template", "autonomy": "notify_only"},
    }]}
    _make_kb(tmp_path, decisions=bad)
    report = await run_validate("mini", project_root=tmp_path)
    assert any("ghost-template" in i.message
               for i in _checks(report, "decision.assets"))
    assert report.exit_code() == 1


async def test_rule_advisory_is_warning(tmp_path):
    """有行动提议却没写建议 → advisory(可写不可静默)。"""
    adv = {"version": 1, "rules": [{
        "id": "adv", "severity": "warning",
        "subject": {"metrics": ["total_amount"]},
        "baseline": {"kind": "literal", "value": 1.0},
        "conditions": ["delta > 0"],
        "action": {"template": "notice", "autonomy": "notify_only"},
    }]}
    _make_kb(tmp_path, decisions=adv)
    (tmp_path / ".trove" / "actions" / "notice").mkdir(parents=True)
    (tmp_path / ".trove" / "actions" / "notice" / "TEMPLATE.md").write_text(
        "---\nname: notice\nstatus: confirmed\n---\n\nbody\n", encoding="utf-8")
    report = await run_validate("mini", project_root=tmp_path)
    advisories = _checks(report, "decision.advisory")
    assert advisories and all(i.severity == "warning" for i in advisories)


# ── skills ───────────────────────────────────────────────


async def test_skill_unknown_frontmatter_key_warns(tmp_path):
    """未知键:读路径宽容(不锁死存量文件),干跑报出来。"""
    _make_skill(tmp_path, "caliber", {
        "name": "caliber", "description": "d", "tier": "required",
        "status": "confirmed", "triggers": {"node": "query_sketch"},
        "prioirty": "high",  # 拼错
    })
    report = await run_validate(project_root=tmp_path)
    hits = _checks(report, "skill.frontmatter")
    assert hits and "prioirty" in hits[0].message
    assert hits[0].severity == "warning"


async def test_skill_bad_tier_and_status_are_errors(tmp_path):
    """tier/status 出闭集 = 没有任何投递面 → 硬错误。"""
    _make_skill(tmp_path, "weird", {
        "name": "weird", "description": "d", "tier": "advisor",
        "status": "archived",
    })
    report = await run_validate(project_root=tmp_path)
    assert _checks(report, "skill.tier")[0].severity == "error"
    assert _checks(report, "skill.status")[0].severity == "error"
    assert report.exit_code() == 1


async def test_skill_validator_bad_expression_is_error(tmp_path):
    """validator 的 checks.expr 预解析失败(拼错的变量名)→ 硬错误。"""
    _make_skill(tmp_path, "no-negative", {
        "name": "no-negative", "description": "d", "tier": "validator",
        "status": "confirmed", "triggers": {"node": "validate"},
        "mode": "deterministic", "severity": "blocking",
        "targets": ["result"],
        "checks": [{"expr": "mn >= 0", "message": "negative values"}],
    })
    report = await run_validate(project_root=tmp_path)
    hits = _checks(report, "skill.validator")
    assert hits and hits[0].severity == "error"
    assert "mn" in hits[0].message


async def test_skill_validator_wrong_host_is_error(tmp_path):
    """validator 声明宿主 gen_sql:result 断言只在 validate 节点跑。"""
    _make_skill(tmp_path, "wrong-host", {
        "name": "wrong-host", "description": "d", "tier": "validator",
        "status": "confirmed", "triggers": {"node": "gen_sql"},
        "mode": "deterministic", "severity": "advisory",
        "targets": ["result"],
        "checks": [{"expr": "row_count > 0"}],
    })
    report = await run_validate(project_root=tmp_path)
    hits = _checks(report, "skill.validator")
    assert hits and hits[0].severity == "error"
    assert "validate" in hits[0].message


async def test_skill_validator_fields_on_other_tier_is_error(tmp_path):
    """validator 字段出现在别的档位 = 永不生效的死配置。"""
    _make_skill(tmp_path, "stray-fields", {
        "name": "stray-fields", "description": "d", "tier": "required",
        "status": "confirmed", "triggers": {"node": "gen_sql"},
        "checks": [{"expr": "row_count > 0"}],
    })
    report = await run_validate(project_root=tmp_path)
    assert any("checks" in i.message
               for i in _checks(report, "skill.validator"))


async def test_skill_available_with_wrong_node_is_error(tmp_path):
    """available 档只在 gen_sql 广告 —— 写别的 node 永远取不到。"""
    _make_skill(tmp_path, "ad", {
        "name": "ad", "description": "d", "tier": "available",
        "status": "confirmed", "triggers": {"node": "conclusion"},
    })
    report = await run_validate(project_root=tmp_path)
    hits = _checks(report, "skill.node")
    assert hits and hits[0].severity == "error"
    assert "gen_sql" in hits[0].message


async def test_skill_required_with_unknown_node_is_error(tmp_path):
    _make_skill(tmp_path, "req", {
        "name": "req", "description": "d", "tier": "required",
        "status": "confirmed", "triggers": {"node": "answer_metadata"},
    })
    report = await run_validate(project_root=tmp_path)
    assert _checks(report, "skill.node")[0].severity == "error"


async def test_skill_name_mismatch_blocks_advertisement(tmp_path):
    """available 广告的是 frontmatter 名,load_skill 按名找目录 → 不一致 = 取不到。"""
    _make_skill(tmp_path, "dir-name", {
        "name": "other-name", "description": "d", "tier": "available",
        "status": "confirmed",
    })
    report = await run_validate(project_root=tmp_path)
    hits = _checks(report, "skill.name")
    assert hits and hits[0].severity == "error"


async def test_skill_duplicate_names_conflict(tmp_path):
    """两个目录声明同一个生效名 → 后者静默失效。"""
    for dir_name in ("one", "two"):
        _make_skill(tmp_path, dir_name, {
            "name": "shared", "description": "d", "tier": "required",
            "status": "confirmed", "triggers": {"node": "gen_sql"},
        })
    report = await run_validate(project_root=tmp_path)
    hits = _checks(report, "skill.conflict")
    assert hits and hits[0].severity == "error"


async def test_skill_broken_frontmatter_is_error(tmp_path):
    """坏的 SKILL.md 不炸整轮:一条 error,其他检查继续。"""
    d = tmp_path / ".trove" / "skills" / "broken"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("no frontmatter here", encoding="utf-8")
    _make_skill(tmp_path, "good", {
        "name": "good", "description": "d", "tier": "required",
        "status": "confirmed", "triggers": {"node": "gen_sql"},
    })
    report = await run_validate(project_root=tmp_path)
    assert _checks(report, "skill.read")
    assert any(m.name == "good" for m in report.mounts)


async def test_skill_empty_body_is_error(tmp_path):
    _make_skill(tmp_path, "empty", {
        "name": "empty", "description": "d", "tier": "required",
        "status": "confirmed", "triggers": {"node": "gen_sql"},
    }, body="")
    report = await run_validate(project_root=tmp_path)
    assert _checks(report, "skill.body")[0].severity == "error"


async def test_pending_skill_previews_mount_but_notes_gate(tmp_path):
    """未确认草稿:挂点照预览(确认后去哪),备注确认门。"""
    _make_skill(tmp_path, "draft", {
        "name": "draft", "description": "d", "tier": "available",
        "status": "pending",
    })
    report = await run_validate(project_root=tmp_path)
    preview = next(m for m in report.mounts if m.name == "draft")
    assert preview.mounts == [
        "gen_sql 可用技能广告(仅描述)+ load_skill 按需加载"]
    assert any("未确认" in n for n in preview.notes)


async def test_global_required_skill_previews_every_node(tmp_path):
    """未声明 triggers.node 的 required 技能 = 全局注入。"""
    _make_skill(tmp_path, "global", {
        "name": "global", "description": "d", "tier": "required",
        "status": "confirmed", "triggers": {"role": "analyst"},
    })
    report = await run_validate(project_root=tmp_path)
    preview = next(m for m in report.mounts if m.name == "global")
    assert len(preview.mounts) == len(SKILL_NODES)
    assert any("role=analyst" in n for n in preview.notes)


# ── 边界:live 关闭时绝不连数据源 ─────────────────────────


async def test_live_off_never_resolves_adapter(tmp_path, monkeypatch):
    _make_kb(tmp_path)
    called = []

    async def _boom(datasource, project_root):
        called.append(datasource)
        raise AssertionError("live 探测在缺省档被调用了")

    monkeypatch.setattr(validate_service, "_resolve_live_adapter", _boom)
    report = await run_validate("mini", project_root=tmp_path)
    assert called == []
    assert _checks(report, "kb.live") == []
    assert report.live is False


async def test_live_probe_closes_connection(tmp_path, monkeypatch):
    """live 探测完必须关连接 —— 这是「进程不退出」那个坑的回归门。

    sqlite 适配器走 aiosqlite,连接背后是一条**非 daemon** worker 线程:
    不关连接,解释器退出时会 join 它,表现是「报告打完、命令挂死」。
    断言直接落在 ``_conn`` 上:泄漏时它是活的 connection 对象。
    """
    import sqlite3

    from trove.core.types import DatasourceConfig
    from trove.services.datasource.registry import ConnectorRegistry

    _make_kb(tmp_path)
    db = tmp_path / "probe.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE loan "
                "(loan_id INTEGER PRIMARY KEY, amount REAL, date TEXT)")
    con.execute("CREATE TABLE branch (branch_id INTEGER PRIMARY KEY, name TEXT)")
    con.commit()
    con.close()

    registry = ConnectorRegistry()
    adapter = await registry.register(DatasourceConfig(
        name="mini", type="sqlite", connection_params={"path": str(db)}))

    async def _fake(datasource, project_root):
        return registry, adapter

    monkeypatch.setattr(validate_service, "_resolve_live_adapter", _fake)
    report = await run_validate("mini", project_root=tmp_path, live=True)
    assert report.live is True
    assert _checks(report, "kb.live") == []      # 探测本身成功
    assert adapter._conn is None                 # 但连接已关
    assert adapter._connected is False


async def test_live_probe_closes_connection_on_probe_failure(tmp_path, monkeypatch):
    """探测抛异常也要关 —— 失败路径不关照样吊住解释器。"""
    import sqlite3

    from trove.core.types import DatasourceConfig
    from trove.services.datasource.registry import ConnectorRegistry

    _make_kb(tmp_path)
    db = tmp_path / "probe.db"
    sqlite3.connect(db).close()

    registry = ConnectorRegistry()
    adapter = await registry.register(DatasourceConfig(
        name="mini", type="sqlite", connection_params={"path": str(db)}))

    async def _fake(datasource, project_root):
        return registry, adapter

    async def _boom(adapter, payloads):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(validate_service, "_resolve_live_adapter", _fake)
    monkeypatch.setattr(validate_service, "check_enums", _boom)
    report = await run_validate("mini", project_root=tmp_path, live=True)
    assert any("探测失败" in i.message for i in _checks(report, "kb.live"))
    assert adapter._conn is None
    assert adapter._connected is False


# ── SKILL_NODES 与运行期调用点同源(pin) ───────────────────


def test_skill_nodes_match_runtime_call_sites():
    """SKILL_NODES 不是第二份真源:它必须等于运行期的调用点集合。

    新增/改名一个注入节点而没更新 SKILL_NODES,validate 的判定会比运行时
    窄(把活的配置报成死的)—— 这条测试就是那个漂移的报警器。
    """
    nodes, hosts = skill_node_call_sites()
    assert nodes == set(SKILL_NODES)
    assert hosts == {"VALIDATOR_HOST"}, (
        "validators_for 的宿主节点必须来自 VALIDATOR_HOST 这一个常量")


async def test_json_shape_is_stable(tmp_path):
    """--json 的顶层键是 CI 契约;文本输出同源(render)。"""
    import json

    _make_kb(tmp_path, decisions=_RULE)
    report = await run_validate("mini", project_root=tmp_path)
    data = report.to_dict()
    assert set(data) == {
        "ok", "errors", "warnings", "project_root", "datasources",
        "counts", "live", "mounts", "envelopes", "issues"}
    assert data["ok"] is True and data["errors"] == 0
    assert json.loads(json.dumps(data, ensure_ascii=False)) == data
    assert "结论:" in report.render()


# ── preset 面 ────────────────────────────────────────────


def _make_preset(tmp_path: Path, name: str, data: dict) -> Path:
    """组织侧 preset(``.trove/presets/<name>/preset.yml``)。"""
    d = tmp_path / ".trove" / "presets" / name
    _write_yaml(d / "preset.yml", data)
    return d


def _preset_checks(report, prefix: str = "preset") -> list:
    return [i for i in report.issues if i.check.startswith(prefix)]


async def test_builtin_example_preset_validates_without_hard_errors(tmp_path):
    """随码分发的示例 preset 必须**无硬错误** —— 它是 preset 该长什么样的
    参照物;一份让 ``trove validate`` 变红的参照物等于没有参照物。

    它的主题域骨架不声明 datasets(那是数据源事实,preset 里写不出来)
    → 由 apply 报 unresolved + 这里报 warning,两级都不是 error。
    """
    report = await run_validate(project_root=tmp_path)
    assert report.errors == []
    domain = [i for i in _preset_checks(report) if i.check == "preset.domain"]
    assert domain and domain[0].severity == "warning"
    assert "financial-analysis" in {m.name for m in report.mounts}


async def test_preset_unknown_key_is_error(tmp_path):
    _make_preset(tmp_path, "typo", {"name": "typo", "description": "d",
                                    "skillz": []})
    report = await run_validate(project_root=tmp_path)
    errs = [i for i in _preset_checks(report) if i.severity == "error"]
    assert errs and errs[0].check == "preset.load" and errs[0].target == "typo"
    assert "合法键" in errs[0].message and "skills" in errs[0].message
    assert report.exit_code() == 1


async def test_preset_name_must_match_directory(tmp_path):
    _make_preset(tmp_path, "dir-name", {"name": "other", "description": "d"})
    report = await run_validate(project_root=tmp_path)
    errs = [i for i in _preset_checks(report) if i.severity == "error"]
    assert errs and "不一致" in errs[0].message


async def test_preset_shadowing_builtin_is_warning(tmp_path):
    _make_preset(tmp_path, "financial-analysis",
                 {"name": "financial-analysis", "version": 7,
                  "description": "组织本地版"})
    report = await run_validate(project_root=tmp_path)
    shadow = [i for i in _preset_checks(report) if i.check == "preset.shadow"]
    assert shadow and shadow[0].severity == "warning"
    assert shadow[0].target == "financial-analysis"
    assert report.exit_code() == 0            # 遮蔽不拦 CI
    assert report.exit_code(strict=True) == 1


async def test_preset_missing_skill_reference_is_error(tmp_path):
    _make_preset(tmp_path, "refs", {
        "name": "refs", "description": "d", "skills": ["no-such-skill"]})
    report = await run_validate(project_root=tmp_path)
    errs = [i for i in _preset_checks(report) if i.check == "preset.ref"]
    assert errs and errs[0].severity == "error"
    assert "no-such-skill" in errs[0].message


async def test_preset_code_skill_reference_resolves(tmp_path):
    _make_preset(tmp_path, "refs", {
        "name": "refs", "description": "d", "skills": ["plan_query"]})
    report = await run_validate(project_root=tmp_path)
    assert [i for i in _preset_checks(report) if i.check == "preset.ref"] == []


async def test_preset_skill_template_create_would_reject_is_error(tmp_path):
    _make_preset(tmp_path, "badskill", {
        "name": "badskill", "description": "d",
        "skills": [{"name": "no-body", "description": "d", "body": ""}]})
    report = await run_validate(project_root=tmp_path)
    errs = [i for i in _preset_checks(report) if i.check == "preset.skill"]
    assert errs and errs[0].severity == "error"
    assert "body" in errs[0].message          # 写入面自己的措辞


async def test_preset_rule_template_condition_is_error(tmp_path):
    """条件语言是闭文法:算术运算符在 lint 就拒(不是等到套用时才发现)。"""
    _make_preset(tmp_path, "badrule", {
        "name": "badrule", "description": "d",
        "decisions": [{
            "id": "r-bad", "window": "last month",
            "subject": {"metrics": ["total_amount"]},
            "baseline": {"kind": "prev_period"},
            "conditions": ["current > baseline * 1.2"],
        }]})
    report = await run_validate(project_root=tmp_path)
    errs = [i for i in _preset_checks(report) if i.check == "preset.rule"]
    assert errs and errs[0].severity == "error"
    assert "r-bad" in errs[0].message


async def test_preset_domain_without_datasets_warns(tmp_path):
    _make_preset(tmp_path, "dom", {
        "name": "dom", "description": "d",
        "domains": [{"name": "credit-risk", "description": "信贷"}]})
    report = await run_validate(project_root=tmp_path)
    warns = [i for i in _preset_checks(report) if i.check == "preset.domain"]
    assert warns and warns[0].severity == "warning"
    assert "unresolved" in warns[0].message
    assert report.exit_code() == 0


async def test_preset_datasource_bound_checks_need_a_datasource(tmp_path):
    """绑定引用只在 ``--datasource`` 下判 —— 且只出 warning。

    preset 不绑定数据源(它的用途是套到**还没接入**的源上),所以「这个源
    还没有这条规则 / 这个指标」不能是硬错误;不点名数据源时干脆不判。
    """
    _make_kb(tmp_path, decisions=_RULE)
    _make_preset(tmp_path, "bound", {
        "name": "bound", "description": "d",
        "decisions": [
            "ghost-rule",                     # 引用:decisions.yml 里没有
            {"id": "r-metric", "window": "last month",
             "subject": {"metrics": ["no_such_metric"]},
             "baseline": {"kind": "prev_period"},
             "conditions": ["delta > 0"]},
        ],
        "domains": [{"name": "credit-risk", "datasets": ["no_such_dataset"]}]})

    loose = await run_validate(project_root=tmp_path)
    # 不点名 → 静态面:这份 preset 一条都不判(内置示例自己那条 domain
    # warning 不在此列 —— 按 target 过滤,只断言本测试写的这份)
    assert [i for i in _preset_checks(loose) if i.target == "bound"] == []

    report = await run_validate("mini", project_root=tmp_path)
    binding = [i for i in _preset_checks(report) if i.target == "bound"]
    assert {i.check for i in binding} == {
        "preset.ref", "preset.metric", "preset.domain"}
    assert {i.severity for i in binding} == {"warning"}
    assert report.exit_code() == 0
    assert all(i.datasource == "mini" for i in binding)


async def test_preset_mount_preview_and_counts(tmp_path):
    _make_preset(tmp_path, "starter", {
        "name": "starter", "description": "d",
        "skills": [{"name": "s1", "description": "d", "body": "b"}],
        "decisions": ["ghost"], "semantics": {"notes": ["n"]}})
    report = await run_validate(project_root=tmp_path)
    preview = next(m for m in report.mounts
                   if m.kind == "preset" and m.name == "starter")
    assert preview.status.startswith("org")
    targets = " | ".join(preview.mounts)
    assert "技能草稿队列" in targets
    assert "decision_drafts.yml" in targets
    assert report.counts["presets"] >= 1
    assert report.counts["preset_items"] >= 3


# ── 信封面(E1) ──────────────────────────────────────────


async def test_report_carries_envelopes(tmp_path):
    """信封是报告的 additive 节:每资产一封,counts 同步。"""
    _make_kb(tmp_path, decisions=_RULE)
    _make_skill(tmp_path, "loan-caliber", {
        "name": "loan-caliber", "description": "How to read loan amounts.",
        "triggers": {"node": "query_sketch"}, "tier": "required",
        "status": "confirmed",
    })
    report = await run_validate("mini", project_root=tmp_path)
    assert report.envelopes
    assert report.counts["envelopes"] == len(report.envelopes)
    kinds = {e["kind"] for e in report.envelopes}
    assert {"skill", "decision", "preset"} <= kinds
    skill = next(e for e in report.envelopes if e["name"] == "loan-caliber")
    assert skill["state"] == "confirmed"
    assert skill["mounts"][0]["node"] == "query_sketch"
    assert "信封" in report.render()
    assert "envelopes" in report.to_dict()


async def test_envelope_unresolved_is_hard_error(tmp_path):
    """推导不出的引用 → envelope.unresolved error —— 死配置不许静默。"""
    _make_kb(tmp_path)
    skills = tmp_path / ".trove" / "skills" / "broken"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("没有 frontmatter 的正文\n",
                                     encoding="utf-8")
    report = await run_validate("mini", project_root=tmp_path)
    hits = [i for i in report.issues if i.check == "envelope.unresolved"]
    assert hits and hits[0].severity == "error"
    assert hits[0].target == "broken"
    assert report.exit_code() == 1
