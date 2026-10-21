"""Human-review and parameter-study authorization for solver submissions.

The store is deliberately file based: approvals are made inside DianaIE, while the solver is launched by the
separate MCP process.  Every uncertain or damaged state fails closed.  A submission permit is bound to the exact
files it authorizes and can be consumed only once; baseline trust and campaign trust are higher-level provenance,
not exemptions from the final file-bound permit.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import config


class GateError(RuntimeError):
    pass


def _id(prefix: str) -> str:
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def state_fingerprint(snapshot: dict[str, Any], dcf: bytes) -> str:
    """Stable identity of the model state shown for review plus the complete analysis commands."""
    def stable(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items() if key not in ("taken", "stored_path")}
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value

    canonical = json.dumps(stable(snapshot), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return _hash_bytes(canonical.encode("utf-8") + b"\0" + dcf)


def _read(path: Path, what: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GateError(f"{what}不存在或已损坏：{path}") from exc
    if not isinstance(value, dict):
        raise GateError(f"{what}格式无效：{path}")
    return value


class GuardStore:
    """Persistent review/baseline/campaign records and one-use solver permits."""

    def __init__(self, root: Path | None = None):
        self._root = root
        self._lock = threading.RLock()

    @property
    def root(self) -> Path:
        return self._root or config.state_dir() / "review-gate"

    def _folder(self, kind: str) -> Path:
        return self.root / kind

    @contextmanager
    def _exclusive(self):
        """Cross-process lock for one-use review permits (several MCP clients may share this store)."""
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".gate.lock"
        with lock_path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - exercised by deployments under Linux/Wine
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _path(self, kind: str, object_id: str) -> Path:
        return self._folder(kind) / object_id / "record.json"

    def record_path(self, kind: str, object_id: str) -> Path:
        return self._path(kind, object_id)

    def _write(self, path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        os.replace(temporary, path)

    def _audit(self, event: str, **fields: Any) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        row = {"time": time.time(), "event": event, **fields}
        with (self.root / "audit.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def create_review(
        self,
        *,
        project: str,
        analysis: str,
        snapshot: dict[str, Any],
        dat: Path,
        dcf: Path,
        diana_version: str | None,
        units: dict[str, Any],
        validation_issues: list[str],
        changes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        dat, dcf = Path(dat).resolve(), Path(dcf).resolve()
        if not dat.is_file() or not dcf.is_file():
            raise GateError("准备人工审阅前必须先导出完整的 .dat 和 .dcf")
        with self._lock:
            review_id = _id("review")
            folder = self._folder("reviews") / review_id
            folder.mkdir(parents=True, exist_ok=False)
            saved_dat, saved_dcf = folder / "model.dat", folder / "analysis.dcf"
            shutil.copyfile(dat, saved_dat)
            shutil.copyfile(dcf, saved_dcf)
            (folder / "snapshot.json").write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
            commands = saved_dcf.read_bytes()
            record: dict[str, Any] = {
                "id": review_id,
                "kind": "review",
                "status": "pending",
                "created": time.time(),
                "project": project,
                "analysis": analysis,
                "diana_version": diana_version,
                "units": units,
                "validation_issues": list(validation_issues),
                "changes": list(changes),
                "snapshot": str(folder / "snapshot.json"),
                "dat": str(saved_dat),
                "dcf": str(saved_dcf),
                "dat_sha256": file_hash(saved_dat),
                "dcf_sha256": _hash_bytes(commands),
                "state_fingerprint": state_fingerprint(snapshot, commands),
            }
            self._write(folder / "record.json", record)
            self._audit("review.created", object_id=review_id, project=project, analysis=analysis)
            return record

    def get_review(self, review_id: str) -> dict[str, Any]:
        return _read(self._path("reviews", review_id), "审阅记录")

    def get_baseline(self, baseline_id: str) -> dict[str, Any]:
        return _read(self._path("baselines", baseline_id), "基准模型")

    def get_campaign(self, campaign_id: str) -> dict[str, Any]:
        return _read(self._path("campaigns", campaign_id), "参数分析方案")

    @staticmethod
    def _require_artifacts(record: dict[str, Any], what: str) -> None:
        try:
            valid = (file_hash(Path(record["dat"])) == record["dat_sha256"] and
                     file_hash(Path(record["dcf"])) == record["dcf_sha256"])
        except (OSError, KeyError) as exc:
            raise GateError(f"{what}文件不存在或记录不完整") from exc
        if not valid:
            raise GateError(f"{what}归档文件已被修改，授权失效")

    def _approved_baseline(self, baseline_id: str) -> dict[str, Any]:
        baseline = self.get_baseline(baseline_id)
        if baseline.get("status") != "approved":
            raise GateError("基准模型尚未通过验证求解和质量检查")
        self._require_artifacts(baseline, "基准模型")
        return baseline

    def list_records(self, kind: str, limit: int = 20) -> list[dict[str, Any]]:
        if kind not in ("reviews", "baselines", "campaigns", "permits"):
            raise GateError(f"未知记录类型：{kind}")
        paths = sorted(self._folder(kind).glob("*/record.json"),
                       key=lambda path: path.stat().st_mtime, reverse=True)
        rows = []
        for path in paths[:max(1, int(limit))]:
            try:
                rows.append(_read(path, kind))
            except GateError:
                continue
        return rows

    def latest_review(self, project: str, analysis: str) -> dict[str, Any] | None:
        return next((row for row in self.list_records("reviews", 200)
                     if row.get("project") == project and row.get("analysis") == analysis), None)

    def gui_decide(self, object_id: str, action: str, confirmation: str) -> dict[str, Any]:
        """Persist a decision made by a callback in the DianaIE review dock.

        This method is intentionally not exposed as an MCP tool.  ``source`` is fixed so an API caller cannot label
        an arbitrary decision as something else; the bridge UI is the supported caller.
        """
        allowed = {
            "approve_once": ("reviews", "approved"),
            "approve_self_check": ("reviews", "approved"),
            "approve_baseline": ("reviews", "approved"),
            "approve_campaign": ("campaigns", "approved"),
            "reject": (None, "rejected"),
        }
        if action not in allowed:
            raise GateError(f"未知人工决定：{action}")
        if not str(confirmation).strip():
            raise GateError("人工决定必须附有确认说明")
        with self._lock:
            folder_hint, status = allowed[action]
            candidates = [folder_hint] if folder_hint else ["reviews", "campaigns"]
            path = next((self._path(kind, object_id) for kind in candidates if self._path(kind, object_id).is_file()), None)
            if path is None:
                raise GateError(f"找不到待审对象 {object_id}")
            record = _read(path, "待审对象")
            if record.get("status") != "pending":
                raise GateError(f"{object_id} 当前状态为 {record.get('status')}，不能重复决定")
            record.update({
                "status": status,
                "decision": action,
                "confirmation": str(confirmation).strip(),
                "decided": time.time(),
                "decision_source": "diana_gui",
            })
            self._write(path, record)
            self._audit(f"{record['kind']}.decided", object_id=object_id, action=action,
                        source="diana_gui", confirmation=record["confirmation"])
            return record

    def _issue(self, *, source_type: str, source_id: str, dat: Path, dcf: Path,
               details: dict[str, Any] | None = None) -> dict[str, Any]:
        dat, dcf = Path(dat).resolve(), Path(dcf).resolve()
        if not dat.is_file() or not dcf.is_file():
            raise GateError("许可对应的模型或求解命令文件不存在")
        permit_id = _id("permit")
        record = {
            "id": permit_id,
            "kind": "permit",
            "status": "issued",
            "issued": time.time(),
            "expires": time.time() + 3600,
            "source_type": source_type,
            "source_id": source_id,
            "dat_sha256": file_hash(dat),
            "dcf_sha256": file_hash(dcf),
            "details": details or {},
        }
        self._write(self._path("permits", permit_id), record)
        self._audit("permit.issued", object_id=permit_id, source_type=source_type, source_id=source_id)
        return record

    @staticmethod
    def _require_self_check(self_check: dict[str, Any]) -> None:
        if not isinstance(self_check, dict) or self_check.get("passed") is not True:
            raise GateError("AI 自检未通过，不能提交计算")

    def permit_review(self, review_id: str, current_state_fingerprint: str, current_dat_sha256: str,
                      self_check: dict[str, Any]) -> dict[str, Any]:
        with self._exclusive(), self._lock:
            record = self.get_review(review_id)
            if record.get("status") == "pending":
                raise GateError("该模型尚未获得 DianaIE GUI 人工批准")
            if record.get("status") != "approved" or record.get("decision") not in (
                "approve_once", "approve_self_check", "approve_baseline"
            ):
                raise GateError("该审阅记录没有本次调试计算授权")
            self._require_artifacts(record, "人工审阅")
            if record.get("decision") in ("approve_self_check", "approve_baseline"):
                self._require_self_check(self_check)
            if record.get("dat_sha256") != current_dat_sha256:
                record["status"] = "stale"
                record["stale_at"] = time.time()
                self._write(self._path("reviews", review_id), record)
                self._audit("review.stale", object_id=review_id, reason="dat changed")
                raise GateError("模型数据在人工审阅后已变化，本次授权失效")
            if record.get("permit_id"):
                permit = _read(self._path("permits", record["permit_id"]), "计算提交许可")
                if permit.get("status") == "issued":
                    return permit
                raise GateError("该人工授权对应的计算许可已经使用，不能重复提交")
            if record.get("state_fingerprint") != current_state_fingerprint:
                record["status"] = "stale"
                record["stale_at"] = time.time()
                self._write(self._path("reviews", review_id), record)
                self._audit("review.stale", object_id=review_id)
                raise GateError("模型或求解设置在人工审阅后已变化，本次授权失效")
            permit = self._issue(source_type="review", source_id=review_id,
                                 dat=Path(record["dat"]), dcf=Path(record["dcf"]),
                                 details={"self_check": self_check, "decision": record.get("decision")})
            record["permit_id"] = permit["id"]
            self._write(self._path("reviews", review_id), record)
            return permit

    def finalize_baseline(self, review_id: str) -> dict[str, Any]:
        with self._lock:
            review = self.get_review(review_id)
            if review.get("status") != "approved" or review.get("decision") != "approve_baseline":
                raise GateError("当前审阅尚未获得“批准为基准模型”的人工决定")
            self._require_artifacts(review, "人工审阅")
            if review.get("baseline_id"):
                return self.get_baseline(review["baseline_id"])
            baseline_id = _id("baseline")
            folder = self._folder("baselines") / baseline_id
            folder.mkdir(parents=True, exist_ok=False)
            for source, name in ((review["dat"], "model.dat"), (review["dcf"], "analysis.dcf"),
                                 (review["snapshot"], "snapshot.json")):
                shutil.copyfile(source, folder / name)
            record = {
                "id": baseline_id,
                "kind": "baseline",
                # Human review authorizes one validation solve.  Trust is promoted to ``approved`` only after that
                # run passes the automatic post-solve quality gate.
                "status": "pending_validation",
                "created": time.time(),
                "review_id": review_id,
                "project": review["project"],
                "analysis": review["analysis"],
                "diana_version": review.get("diana_version"),
                "units": review.get("units") or {},
                "confirmation": review["confirmation"],
                "decision_source": "diana_gui",
                "dat": str(folder / "model.dat"),
                "dcf": str(folder / "analysis.dcf"),
                "snapshot": str(folder / "snapshot.json"),
                "dat_sha256": file_hash(folder / "model.dat"),
                "dcf_sha256": file_hash(folder / "analysis.dcf"),
                "state_fingerprint": review["state_fingerprint"],
            }
            self._write(folder / "record.json", record)
            for path in (folder / "model.dat", folder / "analysis.dcf", folder / "snapshot.json"):
                try:
                    path.chmod(0o444)
                except OSError:
                    pass
            review["baseline_id"] = baseline_id
            self._write(self._path("reviews", review_id), review)
            self._audit("baseline.created", object_id=baseline_id, review_id=review_id)
            return record

    def permit_baseline(self, baseline_id: str) -> dict[str, Any]:
        with self._lock:
            baseline = self._approved_baseline(baseline_id)
            return self._issue(source_type="baseline", source_id=baseline_id,
                               dat=Path(baseline["dat"]), dcf=Path(baseline["dcf"]))

    def create_campaign(self, baseline_id: str, *, name: str, parameters: list[dict[str, Any]],
                        combination: str, self_checks: list[str], quality_checks: list[str]) -> dict[str, Any]:
        self._approved_baseline(baseline_id)
        if not name.strip() or not parameters:
            raise GateError("参数分析方案必须有名称和至少一个参数")
        names = [str(p.get("name") or "").strip() for p in parameters]
        paths = [str(p.get("path") or "").strip() for p in parameters]
        if any(not item for item in names + paths) or len(set(names)) != len(names) or len(set(paths)) != len(paths):
            raise GateError("每个参数必须有唯一的 name 和 path")
        for spec in parameters:
            if "values" not in spec and not ({"min", "max"} <= set(spec)):
                raise GateError(f"参数 {spec['name']} 必须指定 values 或 min/max")
            if "values" in spec and (not isinstance(spec["values"], list) or not spec["values"]):
                raise GateError(f"参数 {spec['name']} 的 values 必须是非空列表")
            if "values" not in spec:
                try:
                    if float(spec["min"]) > float(spec["max"]):
                        raise GateError(f"参数 {spec['name']} 的 min 不能大于 max")
                except (TypeError, ValueError) as exc:
                    raise GateError(f"参数 {spec['name']} 的 min/max 必须是数字") from exc
        with self._lock:
            campaign_id = _id("campaign")
            record = {
                "id": campaign_id,
                "kind": "campaign",
                "status": "pending",
                "created": time.time(),
                "baseline_id": baseline_id,
                "name": name.strip(),
                "parameters": parameters,
                "combination": combination,
                "self_checks": list(self_checks),
                "quality_checks": list(quality_checks),
            }
            self._write(self._path("campaigns", campaign_id), record)
            self._audit("campaign.created", object_id=campaign_id, baseline_id=baseline_id)
            return record

    @staticmethod
    def _allowed(value: Any, spec: dict[str, Any]) -> bool:
        if "values" in spec:
            for allowed in spec["values"]:
                if isinstance(value, (int, float)) and isinstance(allowed, (int, float)):
                    if math.isclose(float(value), float(allowed), rel_tol=1e-9, abs_tol=1e-12):
                        return True
                elif value == allowed:
                    return True
            return False
        try:
            number = float(value)
            return float(spec["min"]) <= number <= float(spec["max"])
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _path_is_within(change: str, approved: str) -> bool:
        change = "/".join(part for part in str(change).replace("\\", "/").split("/") if part).casefold()
        approved = "/".join(part for part in str(approved).replace("\\", "/").split("/") if part).casefold()
        return change == approved or change.startswith(approved + "/")

    def permit_variant(self, campaign_id: str, dat: Path, dcf: Path, parameters: dict[str, Any],
                       *, changes: list[str], self_check: dict[str, Any]) -> dict[str, Any]:
        self._require_self_check(self_check)
        with self._lock:
            campaign = self.get_campaign(campaign_id)
            if campaign.get("status") != "approved" or campaign.get("decision") != "approve_campaign":
                raise GateError("参数分析方案尚未获得 DianaIE GUI 人工批准")
            self._approved_baseline(str(campaign["baseline_id"]))
            specs = {str(spec["name"]): spec for spec in campaign["parameters"]}
            if set(parameters) != set(specs):
                missing = sorted(set(specs) - set(parameters))
                extra = sorted(set(parameters) - set(specs))
                raise GateError(f"参数清单与批准方案不一致；缺少 {missing}，多出 {extra}")
            outside = [name for name, value in parameters.items() if not self._allowed(value, specs[name])]
            if outside:
                raise GateError("参数超出人工批准范围：" + ", ".join(outside))
            approved_paths = [str(spec["path"]) for spec in specs.values()]
            undeclared = [change for change in changes
                          if not any(self._path_is_within(change, path) for path in approved_paths)]
            if undeclared:
                raise GateError("变体包含未获批准的模型变化：" + ", ".join(undeclared))
            details = {
                "parameters": parameters,
                "changes": changes,
                "self_check": self_check,
                "baseline_id": campaign["baseline_id"],
                "quality_checks": list(campaign.get("quality_checks") or []),
            }
            permit = self._issue(source_type="variant", source_id=campaign_id, dat=dat, dcf=dcf, details=details)
            self._audit("variant.checked", object_id=permit["id"], campaign_id=campaign_id,
                        parameters=parameters, changes=changes, passed=True)
            return permit

    def _checked_permit(self, permit_id: str | None, dat: Path, dcf: Path) -> tuple[Path, dict[str, Any]]:
        if not permit_id:
            raise GateError("没有计算提交许可；必须先完成人工审阅或使用已批准参数方案")
        path = self._path("permits", permit_id)
        permit = _read(path, "计算提交许可")
        if permit.get("status") == "consumed":
            raise GateError("计算提交许可已使用，不能重放")
        if permit.get("status") != "issued":
            raise GateError(f"计算提交许可状态无效：{permit.get('status')}")
        if time.time() > float(permit.get("expires") or 0):
            raise GateError("计算提交许可已过期")
        if file_hash(Path(dat)) != permit.get("dat_sha256") or file_hash(Path(dcf)) != permit.get("dcf_sha256"):
            raise GateError("待提交文件与已审阅/自检的内容不一致")
        return path, permit

    def validate_permit(self, permit_id: str | None, dat: Path, dcf: Path) -> dict[str, Any]:
        with self._lock:
            return dict(self._checked_permit(permit_id, Path(dat), Path(dcf))[1])

    def get_permit(self, permit_id: str) -> dict[str, Any]:
        return _read(self._path("permits", permit_id), "计算提交许可")

    def consume_permit(self, permit_id: str | None, dat: Path, dcf: Path) -> dict[str, Any]:
        with self._exclusive(), self._lock:
            path, permit = self._checked_permit(permit_id, Path(dat), Path(dcf))
            permit["status"] = "consumed"
            permit["consumed"] = time.time()
            self._write(path, permit)
            self._audit("permit.consumed", object_id=permit["id"], source_type=permit["source_type"],
                        source_id=permit["source_id"])
            return permit

    def record_submission(self, permit: dict[str, Any], job_id: str) -> None:
        self._audit("submission.started", object_id=job_id, permit_id=permit["id"],
                    source_type=permit["source_type"], source_id=permit["source_id"])

    def record_quality(self, job_id: str, passed: bool, checks: list[Any], permit_id: str | None = None) -> None:
        """Record post-solve evidence and, for a baseline candidate, promote trust only after it passes."""
        with self._lock:
            permit = self.get_permit(permit_id) if permit_id else None
            baseline_id = None
            if permit and permit.get("source_type") == "review":
                review = self.get_review(str(permit["source_id"]))
                if review.get("decision") == "approve_baseline":
                    baseline = self.finalize_baseline(review["id"])
                    baseline_id = baseline["id"]
                    baseline["status"] = "approved" if passed else "validation_failed"
                    baseline["validation_job_id"] = job_id
                    baseline["validated"] = time.time()
                    baseline["quality_checks"] = list(checks)
                    self._write(self._path("baselines", baseline_id), baseline)
            required_rules = list(((permit or {}).get("details") or {}).get("quality_checks") or [])
            self._audit("quality.checked", object_id=job_id, permit_id=permit_id,
                        baseline_id=baseline_id, passed=bool(passed), checks=list(checks),
                        required_rules=required_rules)
