"""Cross-message Case transactions. PostgreSQL is required for this workflow.

The existing tickets row is the identity/legacy projection. All Case decisions
come from ticket_case_state. No model output can change facts, actor or versions.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.actor import ActorContext
from app.core.business_facts import fresh, language_of, verified_cells
from app.core.metrics import CASE_ACTIONS, CASE_REJECTIONS
from app.core.policy_scope import PolicyScope
from app.runs.records import RunRecord, new_run_id
from app.runs.store import _COLUMNS, _NOW, _record

ACTIVE = {"open", "waiting_for_info", "in_progress"}
STATUSES = ACTIVE | {"resolved", "cancelled"}
CASE_SUFFIX = ":case-v3"


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decode(value, default=None):
    return json.loads(value) if isinstance(value, str) else (value if value is not None else default)


def conflict(code: str) -> HTTPException:
    CASE_REJECTIONS.labels(reason=code).inc()
    return HTTPException(409, {"code": code, "message": "工单已变化，请刷新后重新提交；原输入未被覆盖"})


def pending_fields(c) -> list[str]:
    """Server requirements cannot be cleared by a caller or a model."""
    facts = decode(c["facts"], {})
    required = {"order": ["order_ref"], "missing_parts": ["order_ref", "model", "quantity"],
                "wrong_item": ["order_ref", "expected_model", "received_model"]}.get(c["issue_type"], [])
    missing = list(dict.fromkeys([*decode(c["pending_fields"], []), *required]))
    return [f for f in missing if f.startswith("image:") or (f not in facts and not (f == "order_ref" and decode(c["order_refs"], [])))]


class CaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    message: str = Field(default="", max_length=4000)
    conv_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    case_id: str | None = Field(default=None, max_length=128)
    new_case: bool = False
    expected_version: int | None = Field(default=None, ge=1)
    expected_control_version: int | None = Field(default=None, ge=1)
    issue_type: Literal["general", "order", "missing_parts", "wrong_item"] = "general"
    customer_ref: str = Field(default="", max_length=200)
    order_refs: list[str] = Field(default_factory=list, max_length=20)
    facts: dict[str, str] = Field(default_factory=dict, max_length=40)
    pending_fields: list[str] | None = Field(default=None, max_length=40)
    policy_scope: PolicyScope | None = None
    attachment_ids: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_input(self):
        if any(k.startswith("image:") for k in [*self.facts, *(self.pending_fields or [])]):
            raise ValueError("图片核验状态由服务器维护，请补充材料或重新核对")
        if self.message_id.startswith(("reply_", "adopt_", "verified_")):
            raise ValueError("message_id 使用了保留前缀")
        if bool(self.case_id) == self.new_case:
            raise ValueError("明确选择现有工单或新建问题；不按订单号自动合并")
        if self.case_id and (self.expected_version is None or self.expected_control_version is None):
            raise ValueError("现有工单需要材料版本和控制权版本")
        values = [*self.order_refs, *self.facts, *self.facts.values(), *(self.pending_fields or []), *self.attachment_ids]
        if any(not v.strip() or len(v) > 1000 or "\x00" in v for v in values):
            raise ValueError("材料字段不能为空、含空字符或超过 1000 字符")
        if "\x00" in self.message or "\x00" in self.customer_ref:
            raise ValueError("输入含无法存储的字符")
        if not (self.message.strip() or self.attachment_ids or self.facts):
            raise ValueError("请提供文字或材料")
        return self


class CaseMutation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    expected_control_version: int = Field(ge=1)
    operation: Literal["status", "takeover", "release", "reopen"]
    status: str = ""
    reason: str = Field(default="", max_length=1000)


class DraftApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    expected_control_version: int = Field(ge=1)
    body_hash: str


class CaseAdoption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_ticket_updated_at: float
    reason: str = Field(min_length=1, max_length=1000)


class CaseService:
    def __init__(self, db, instance: str, *, run_service=None, webhook_url: str = "", attachment_resolver=None,
                 attachments=None, knowledge_base=None, actors=None, identity_store=None):
        self.db = db
        self.instance = instance
        self.run_instance = instance + CASE_SUFFIX
        self.runs = run_service
        self.webhook_url = webhook_url
        # Stage 3: reauthorize IDs for this actor; return {id, sha256}, never URLs.
        self.attachment_resolver = attachment_resolver
        self.attachments = attachments
        self.knowledge_base, self.actors = knowledge_base, actors
        self.identity_store = identity_store

    def authorize(self, actor: ActorContext):
        actor.require_employee()
        if actor.instance != self.instance:
            raise HTTPException(404, "Not Found")

    async def fresh_actor(self, actor, *, conn=None):
        actor = await self.actors.refresh(actor, conn=conn) if self.actors else actor
        self.authorize(actor)
        return actor

    @staticmethod
    def operator(actor, case, *, takeover=False):
        actor.require_role("operations")
        owner = case.get("owner_actor_id")
        if owner and owner != actor.actor_id and "admin" not in actor.roles:
            raise HTTPException(403, "此工单由其他运营人员负责；需管理员重新接管")

    async def _locked(self, conn, case_id: str, actor: ActorContext | None = None):
        # All writers (including the legacy trigger) lock parent then sidecar.
        await conn.fetchrow("SELECT id FROM tickets WHERE id=$1 FOR UPDATE", case_id)
        row = await conn.fetchrow("SELECT * FROM ticket_case_state WHERE case_id=$1 FOR UPDATE", case_id)
        if row is None or row["instance"] != self.instance:
            raise HTTPException(404, "Not Found")
        if actor is not None:
            self.authorize(actor)
        return dict(row)

    def _expected(self, c, version, control):
        if c["version"] != version:
            raise conflict("case_version_changed")
        if c["control_version"] != control:
            raise conflict("case_control_changed")

    async def _event(self, conn, c, kind, actor_id=None, data=None):
        await conn.execute(
            f"INSERT INTO case_events(case_id,actor_id,kind,version,control_version,data,created_at)"
            f" VALUES($1,$2,$3,$4,$5,$6::jsonb,{_NOW})",
            c["case_id"], actor_id, kind, c["version"], c["control_version"], dumps(data or {}),
        )

    async def _save(self, conn, c, *, invalidate=""):
        await conn.execute(
            f"UPDATE ticket_case_state SET instance=$2,managed=TRUE,status=$3,issue_type=$4,customer_ref=$5,"
            f" order_refs=$6::jsonb,owner_actor_id=$7,control_mode=$8,version=$9,control_version=$10,"
            f" facts=$11::jsonb,pending_fields=$12::jsonb,updated_at={_NOW} WHERE case_id=$1",
            c["case_id"], self.instance, c["status"], c["issue_type"], c["customer_ref"],
            dumps(decode(c["order_refs"], [])), c["owner_actor_id"], c["control_mode"], c["version"],
            c["control_version"], dumps(decode(c["facts"], {})), dumps(decode(c["pending_fields"], [])),
        )
        await conn.execute("UPDATE ticket_case_state SET policy_scope=$2::jsonb WHERE case_id=$1",
                           c["case_id"], dumps(decode(c.get("policy_scope"), {})))
        if invalidate:
            await conn.execute(
                "UPDATE case_drafts SET status='invalid',invalid_reason=$2 WHERE case_id=$1 AND status<>'invalid'",
                c["case_id"], invalidate,
            )
        legacy = {"waiting_for_info": "open", "in_progress": "assigned"}.get(c["status"], c["status"])
        await conn.execute("SELECT set_config('ai_helpdesk.case_write','on',true)")
        await conn.execute(f"UPDATE tickets SET status=$2,updated_at={_NOW} WHERE id=$1", c["case_id"], legacy)
        await conn.execute("SELECT set_config('ai_helpdesk.case_write','off',true)")

    async def adopt(self, actor: ActorContext, case_id: str, body: CaseAdoption):
        """Explicit administrator migration of an unknown historical ticket."""
        actor = await self.fresh_actor(actor)
        if "admin" not in actor.roles:
            raise HTTPException(403, "只有管理员可以确认历史工单的归属")
        if not body.reason.strip():
            raise HTTPException(422, "请记录归属依据")
        async with self.db.transaction() as conn:
            actor = await self.fresh_actor(actor, conn=conn)
            actor.require_role("admin")
            ticket = await conn.fetchrow("SELECT * FROM tickets WHERE id=$1 FOR UPDATE", case_id)
            row = await conn.fetchrow("SELECT * FROM ticket_case_state WHERE case_id=$1 FOR UPDATE", case_id)
            if not ticket or not row or row["instance"] != "":
                raise HTTPException(404, "Not Found")
            if ticket["updated_at"] != body.expected_ticket_updated_at:
                raise conflict("case_version_changed")
            c = dict(row)
            c["version"] += 1
            c["owner_actor_id"] = actor.actor_id
            await self._save(conn, c, invalidate="adopted")
            if ticket["last_message"]:
                # Only the ticket's own source message is attributable. Never
                # attach all conversation messages to one of several problems.
                seq = await conn.fetchval(
                    f"INSERT INTO conversation_log(conv_id,user_id,role,content,created_at)"
                    f" VALUES($1,$2,'user',$3,{_NOW}) RETURNING seq",
                    ticket["conv_id"], actor.actor_id, "历史工单记录：" + ticket["last_message"])
                await conn.execute("INSERT INTO conversation_log_turns(seq,instance) VALUES($1,$2)", seq, self.run_instance)
                await conn.execute("INSERT INTO case_messages VALUES($1,$2,$3,$4,$5,$6,'',$7,$8)",
                                   "adopt_" + case_id, case_id, self.instance, ticket["conv_id"], actor.actor_id,
                                   seq, c["version"], hashlib.sha256(ticket["last_message"].encode()).hexdigest())
            await self._event(conn, c, "adopted", actor.actor_id, {"reason": body.reason, "instance": self.instance})
        return await self.get(actor, case_id)

    async def accept_input(self, actor: ActorContext, body: CaseInput, *, request_id="", transport="case"):
        actor = await self.fresh_actor(actor)
        if body.policy_scope is not None:
            actor.require_policy(body.policy_scope)
        digest = hashlib.sha256(dumps({
            "actor": actor.actor_id, "input": body.model_dump(mode="json"),
            "updated_fields": sorted(body.model_fields_set & {"customer_ref", "order_refs"}),
        }).encode()).hexdigest()
        attachments = []
        if body.attachment_ids:
            if self.attachment_resolver is None:
                raise HTTPException(422, {"code": "attachments_not_connected", "message": "附件服务尚未接入"})
            attachments = await self.attachment_resolver(actor, list(dict.fromkeys(body.attachment_ids)))
            if {a["id"] for a in attachments} != set(body.attachment_ids) or any(not a.get("sha256") for a in attachments):
                raise HTTPException(422, "附件校验未完成")
        async with self.db.transaction() as conn:
            actor = await self.fresh_actor(actor, conn=conn)
            if self.identity_store and body.new_case:
                await self.identity_store.conversation(actor, body.conv_id, conn=conn)
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", "case-input:" + body.message_id)
            prior = await conn.fetchrow("SELECT * FROM case_messages WHERE message_id=$1", body.message_id)
            if prior:
                if prior["actor_id"] != actor.actor_id or prior["instance"] != actor.instance:
                    raise HTTPException(404, "Not Found")
                if prior["request_hash"] != digest:
                    raise conflict("message_id_conflict")
                return {"case_id": prior["case_id"], "message_id": body.message_id, "case_version": prior["version"],
                        "run_id": prior["run_id"], "deduplicated": True}
            if self.runs is not None:
                backlog = await self.runs.backlog()
                if backlog is not None and backlog >= self.runs.config.queue_max:
                    raise HTTPException(429, "当前咨询量较大，请稍后重试", headers={"Retry-After": "5"})
            case_id = body.case_id or "tk_" + uuid.uuid4().hex[:12]
            if body.new_case:
                await conn.execute(
                    f"INSERT INTO tickets(id,conv_id,user_id,reason,last_message,status,created_at,updated_at)"
                    f" VALUES($1,$2,$3,'员工创建的业务问题',$4,'open',{_NOW},{_NOW})",
                    case_id, body.conv_id, "case", "请在员工工单入口查看材料",
                )
                await conn.execute("UPDATE ticket_case_state SET instance=$2 WHERE case_id=$1", case_id, self.instance)
            c = await self._locked(conn, case_id, actor)
            actor.require_policy(body.policy_scope or decode(c.get("policy_scope"), {}))
            ticket_conv = await conn.fetchval("SELECT conv_id FROM tickets WHERE id=$1", case_id)
            if ticket_conv != body.conv_id:
                raise HTTPException(404, "工单不属于此会话")
            if body.case_id:
                self._expected(c, body.expected_version, body.expected_control_version)
            else:
                c.update(version=0, control_mode="assistant", issue_type=body.issue_type,
                         customer_ref=body.customer_ref, order_refs=body.order_refs)
                await conn.execute("UPDATE ticket_case_state SET created_by=$2 WHERE case_id=$1", case_id, actor.actor_id)
            if c["status"] not in ACTIVE:
                raise conflict("case_closed")
            if attachments and self.attachments:
                # Same lock as discard/GC. No deletion can pass between the
                # ready/hash check and the association's commit.
                fresh = await self.attachments.resolve(actor, body.attachment_ids, conn=conn)
                if fresh != sorted(attachments, key=lambda a: a["id"]):
                    raise conflict("attachment_changed")
            # Hashes are immutable. Repeated attachment associations do not count as new material.
            added = []
            for a in attachments:
                prior_hash = await conn.fetchval(
                    "SELECT sha256 FROM case_materials WHERE case_id=$1 AND attachment_id=$2", case_id, a["id"])
                if prior_hash and prior_hash != a["sha256"]:
                    raise conflict("attachment_changed")
                if not prior_hash:
                    added.append(a)
            facts = decode(c["facts"], {})
            changed = bool(body.message.strip() or added)
            if body.policy_scope is not None and body.policy_scope.model_dump(mode="json") != decode(c.get("policy_scope"), {}):
                c["policy_scope"] = body.policy_scope.model_dump(mode="json")
                changed = True
            for field in ("customer_ref", "order_refs"):
                current = decode(c[field], []) if field == "order_refs" else c[field]
                if field in body.model_fields_set and current != getattr(body, field):
                    c[field], changed = getattr(body, field), True
            for field, value in body.facts.items():
                if facts.get(field, {}).get("value") != value:
                    facts[field] = {"value": value, "source": "employee_material", "message_id": body.message_id,
                                    "actor_id": actor.actor_id}
                    changed = True
            if body.pending_fields is not None and body.pending_fields != decode(c["pending_fields"], []):
                protected = [f for f in decode(c["pending_fields"], []) if f.startswith("image:")]
                c["pending_fields"], changed = list(dict.fromkeys([*protected, *body.pending_fields])), True
            c["facts"] = facts
            c["pending_fields"] = pending_fields(c)
            if attachments and self.attachments:
                c["pending_fields"] = list(dict.fromkeys([*c["pending_fields"], "image:review"]))
            c["version"] += int(changed)
            await self._save(conn, c, invalidate="materials_changed" if changed else "")
            run_id = new_run_id()
            content = body.message or f"补充了工单材料（图片 {len(set(body.attachment_ids))} 张，字段 {len(body.facts)} 项）。"
            seq = await conn.fetchval(
                f"INSERT INTO conversation_log(conv_id,user_id,request_id,role,content,created_at)"
                f" VALUES($1,$2,$3,'user',$4,{_NOW}) RETURNING seq",
                body.conv_id, actor.actor_id, request_id, content,
            )
            await conn.execute("INSERT INTO conversation_log_turns(seq,instance) VALUES($1,$2)", seq, self.run_instance)
            await conn.execute(
                "INSERT INTO case_messages(message_id,case_id,instance,conv_id,actor_id,seq,run_id,version,request_hash)"
                " VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                body.message_id, case_id, self.instance, body.conv_id, actor.actor_id, seq, run_id, c["version"], digest,
            )
            for a in added:
                await conn.execute(
                    "INSERT INTO case_materials(case_id,attachment_id,sha256,message_id,actor_id,version)"
                    " VALUES($1,$2,$3,$4,$5,$6)", case_id, a["id"], a["sha256"], body.message_id, actor.actor_id, c["version"])
            if self.attachments:
                for a in attachments:
                    await conn.execute("INSERT INTO message_attachments VALUES($1,$2,$3)", body.message_id, a["id"], a["sha256"])
            snapshot = {k: decode(c[k]) if k in {"facts", "pending_fields", "order_refs"} else c[k]
                        for k in ("case_id", "status", "issue_type", "customer_ref", "order_refs",
                                  "facts", "pending_fields", "control_mode")}
            snapshot["language"] = language_of(body.message) if body.message.strip() else "zh"
            snapshot["policy_scope"] = decode(c.get("policy_scope"), {})
            snapshot["materials"] = [dict(r) for r in await conn.fetch(
                "SELECT attachment_id,sha256,message_id FROM case_materials WHERE case_id=$1 ORDER BY version DESC,attachment_id", case_id)]
            # A bounded batch. On a text supplement, retry the latest batch with
            # the new order/facts context. Older images remain in the audit.
            snapshot["review_materials"] = ([m for m in snapshot["materials"] if m["attachment_id"] in body.attachment_ids]
                                             if attachments else snapshot["materials"][:4])
            snapshot["messages"] = [dict(r) for r in await conn.fetch(
                "SELECT l.role,l.content FROM case_messages m JOIN conversation_log l ON l.seq=m.seq"
                " WHERE m.case_id=$1 AND l.role='user' ORDER BY l.seq DESC LIMIT 12", case_id)][::-1]
            context = {"case_id": case_id, "message_id": body.message_id, "expected_case_version": c["version"],
                       "expected_control_version": c["control_version"], "actor_id": actor.actor_id,
                       "roles": sorted(actor.roles), "instance": actor.instance, "auth_version": actor.auth_version, "snapshot": snapshot}
            await conn.execute(
                f"INSERT INTO agent_runs(run_id,instance,idempotency_key,conv_id,user_id,message,request_id,transport,"
                f"case_context,created_at,updated_at,dispatched_at)"
                f" VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,{_NOW},{_NOW},{_NOW})",
                run_id, self.run_instance, body.message_id, body.conv_id, actor.actor_id, content,
                request_id, transport, dumps(context),
            )
            await self._event(conn, c, "input_accepted", actor.actor_id,
                              {"message_id": body.message_id, "run_id": run_id, "changed": changed})
        if self.runs:
            try:
                await self.runs.broker.dispatch(run_id)
            except Exception:
                pass  # durable queued row is redispatched by the existing sweep
        return {"case_id": case_id, "message_id": body.message_id, "case_version": c["version"],
                "run_id": run_id, "deduplicated": False}

    async def get(self, actor: ActorContext, case_id: str):
        actor = await self.fresh_actor(actor)
        if self.attachments:
            actor = await self.attachments.actor(actor)
        self.authorize(actor)
        async with self.db.transaction() as conn:
            # Messages, reviews, version and Run status must describe one snapshot.
            # Otherwise a terminal status read after a newer commit can stop UI
            # polling while the earlier messages query still lacks that reply.
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            return await self._get(conn, actor, case_id)

    async def _get(self, conn, actor, case_id):
        row = await conn.fetchrow(
            "SELECT t.id,t.conv_id,t.user_id,t.reason,t.last_message,t.created_at,c.*"
            " FROM tickets t JOIN ticket_case_state c ON c.case_id=t.id"
            " WHERE t.id=$1 AND c.instance=$2", case_id, self.instance)
        if not row:
            raise HTTPException(404, "Not Found")
        c = dict(row)
        actor.require_policy(decode(c.get("policy_scope"), {}))
        for k in ("facts", "pending_fields", "order_refs", "policy_scope"):
            c[k] = decode(c[k])
        c["messages"] = [dict(r) for r in await conn.fetch(
            "SELECT l.seq,l.role,l.content,l.created_at,l.turn_id,m.message_id,m.run_id,m.version"
            " FROM (SELECT c.*,COALESCE(t.turn_id,'') AS turn_id FROM conversation_log c"
            " LEFT JOIN conversation_log_turns t ON t.seq=c.seq) l"
            " JOIN case_messages m ON m.seq=l.seq WHERE m.case_id=$1 ORDER BY l.seq", case_id)]
        c["drafts"] = [dict(r) for r in await conn.fetch(
            "SELECT * FROM case_drafts WHERE case_id=$1 ORDER BY created_at DESC", case_id)]
        for d in c["drafts"]:
            d["evidence"] = decode(d["evidence"], [])
            d["valid"] = (d["status"] != "invalid" and d["version"] == c["version"] and d["control_version"] == c["control_version"]
                          and d["evidence_version"] == 1 and await self._evidence_valid(d["evidence"], conn=conn, lock=False))
        c["materials"] = [dict(r) for r in await conn.fetch(
            "SELECT attachment_id,sha256,message_id,version FROM case_materials WHERE case_id=$1", case_id)]
        if self.attachments:
            for material in c["materials"]:
                row = await self.attachments.get(actor, material["attachment_id"], ready=False, conn=conn, lock=False)
                material["attachment"] = self.attachments.public(row)
            c["image_reviews"] = [dict(r) for r in await conn.fetch(
                "SELECT * FROM case_image_reviews WHERE case_id=$1 ORDER BY created_at", case_id)]
            for review in c["image_reviews"]:
                review["result"] = decode(review["result"])
            for message in c["messages"]:
                message["attachment_ids"] = [r["attachment_id"] for r in await conn.fetch(
                    "SELECT attachment_id FROM message_attachments WHERE message_id=$1 ORDER BY attachment_id", message["message_id"])]
        c["events"] = [dict(r) for r in await conn.fetch(
            "SELECT * FROM case_events WHERE case_id=$1 ORDER BY id", case_id)]
        for e in c["events"]:
            e["data"] = decode(e["data"])
        c["runs"] = [dict(r) for r in await conn.fetch(
            "SELECT m.run_id,r.status,r.attempt,r.error,receipt.applied,receipt.reason"
            " FROM case_messages m LEFT JOIN agent_runs r ON r.run_id=m.run_id"
            " LEFT JOIN case_run_receipts receipt ON receipt.run_id=m.run_id"
            " WHERE m.case_id=$1 AND left(m.message_id,6)<>'reply_' AND m.run_id<>'' ORDER BY m.seq", case_id)]
        return c

    async def list(self, actor: ActorContext, conv_id: str | None = None):
        actor = await self.fresh_actor(actor)
        rows = await self.db.fetch(
            "SELECT t.id,t.conv_id,t.last_message,t.created_at,c.* FROM tickets t"
            " JOIN ticket_case_state c ON c.case_id=t.id WHERE c.instance=$1"
            " AND ($2::text IS NULL OR t.conv_id=$2) ORDER BY c.updated_at DESC LIMIT 200",
            self.instance, conv_id)
        result = []
        for row in rows:
            c = dict(row)
            try:
                actor.require_policy(decode(c.get("policy_scope"), {}))
            except HTTPException:
                continue
            for k in ("facts", "order_refs", "pending_fields"):
                c[k] = decode(c[k])
            result.append(c)
        return {"tickets": result}

    async def mutate(self, actor: ActorContext, case_id: str, body: CaseMutation):
        async with self.db.transaction() as conn:
            actor = await self.fresh_actor(actor, conn=conn)
            c = await self._locked(conn, case_id, actor)
            actor.require_policy(decode(c.get("policy_scope"), {}))
            self.operator(actor, c)
            self._expected(c, body.expected_version, body.expected_control_version)
            if body.operation in ("takeover", "release"):
                c["control_mode"] = "human" if body.operation == "takeover" else "assistant"
                c["owner_actor_id"] = actor.actor_id if body.operation == "takeover" else None
                c["control_version"] += 1
            elif body.operation == "reopen":
                if c["status"] in ACTIVE or not body.reason.strip():
                    raise HTTPException(422, "只有已结束的问题可以重开，且必须填写原因")
                c["status"], c["version"] = "open", c["version"] + 1
            else:
                if c["status"] not in ACTIVE or body.status not in STATUSES:
                    raise HTTPException(422, "不允许此状态变化；重开请填写原因")
                c["status"], c["version"] = body.status, c["version"] + 1
            await self._save(conn, c, invalidate=body.operation)
            await self._event(conn, c, body.operation, actor.actor_id, {"status": c["status"], "reason": body.reason})
        return await self.get(actor, case_id)

    async def _evidence_valid(self, evidence, *, conn=None, actor=None, case=None, lock=True):
        refs = [e for e in evidence if e.get("doc_id")]
        if refs and (self.knowledge_base is None or not await self.knowledge_base.validate_policy_refs(refs, conn=conn, lock=lock)):
            return False
        for entry in evidence:
            if entry.get("source") == "business_query" and entry.get("evidence"):
                cells, issues = verified_cells(entry.get("evidence_payload") or {})
                if issues or cells != entry["evidence"]:
                    return False
            if entry.get("source") == "verified_image":
                source = entry.get("business_source") or {}
                if not fresh(source.get("observed_at")) or not entry.get("evidence_id") or entry.get("db_value") is None:
                    return False
                if self.attachments and actor:
                    try:
                        row = await self.attachments.get(actor, entry["attachment_id"], conn=conn)
                        if row["sha256"] != entry["sha256"]:
                            return False
                    except HTTPException:
                        return False
            if entry.get("source") == "verified_evidence":
                # Legacy generic evidence has no business unit/time/provenance.
                return False
        return True

    async def approve(self, actor: ActorContext, case_id: str, draft_id: str, body: DraftApproval):
        async with self.db.transaction() as conn:
            actor = await self.fresh_actor(actor, conn=conn)
            c = await self._locked(conn, case_id, actor)
            actor.require_policy(decode(c.get("policy_scope"), {}))
            self.operator(actor, c)
            self._expected(c, body.expected_version, body.expected_control_version)
            d = await conn.fetchrow("SELECT * FROM case_drafts WHERE id=$1 AND case_id=$2 FOR UPDATE", draft_id, case_id)
            if not d:
                raise HTTPException(404, "Not Found")
            if (d["status"] == "invalid" or d["version"] != c["version"] or
                    d["control_version"] != c["control_version"] or d["body_hash"] != body.body_hash or c["status"] not in ACTIVE):
                raise conflict("draft_stale")
            if d["evidence_version"] != 1 or not await self._evidence_valid(decode(d["evidence"], []), conn=conn, actor=actor, case=c):
                raise conflict("draft_evidence_stale")
            if d["status"] != "approved":
                await conn.execute("SELECT set_config('ai_helpdesk.evidence_approval','on',true)")
                await conn.execute("UPDATE case_drafts SET status='approved',approved_by=$2 WHERE id=$1", draft_id, actor.actor_id)
                await self._event(conn, c, "draft_approved", actor.actor_id, {"draft_id": draft_id, "body_hash": d["body_hash"]})
        return await self.get(actor, case_id)

    async def submit_evidence(self, actor: ActorContext, case_id: str, *, expected_version: int,
                              expected_control_version: int, attachment_id: str, sha256: str,
                              facts: dict[str, str], evidence_id: str):
        """Internal stage-3/4 hook for verified facts; never exposed as client-supplied verification.

        Call only after the trusted extractor/verifier checked the business source.
        Authorization and immutable attachment hash are rechecked at adoption.
        """
        actor = await self.fresh_actor(actor)
        if (not evidence_id or len(evidence_id) > 256 or not facts or len(facts) > 40
                or any(not isinstance(v, str) or not v.strip() or len(v) > 1000 or "\x00" in v
                       for v in [*facts, *facts.values()])):
            raise HTTPException(422, "核实材料必须包含非空字段和稳定的证据编号")
        if self.attachment_resolver is None:
            raise HTTPException(422, "附件服务尚未接入")
        refs = await self.attachment_resolver(actor, [attachment_id])
        if not refs or refs[0].get("sha256") != sha256:
            raise conflict("attachment_changed")
        async with self.db.transaction() as conn:
            actor = await self.fresh_actor(actor, conn=conn)
            c = await self._locked(conn, case_id, actor)
            actor.require_policy(decode(c.get("policy_scope"), {}))
            key = f"evidence:{case_id}:{evidence_id}"
            payload = {"attachment_id": attachment_id, "sha256": sha256, "facts": facts}
            previous = await conn.fetchval("SELECT data FROM case_actions WHERE action_key=$1", key)
            if previous:
                saved = decode(previous)
                if {k: v for k, v in saved.items() if k != "result_version"} != payload:
                    raise conflict("evidence_id_conflict")
                return saved["result_version"]
            self._expected(c, expected_version, expected_control_version)
            material = await conn.fetchrow(
                "SELECT * FROM case_materials WHERE case_id=$1 AND attachment_id=$2 AND sha256=$3",
                case_id, attachment_id, sha256)
            if not material or c["status"] not in ACTIVE:
                raise conflict("evidence_source_changed")
            confirmed = decode(c["facts"], {})
            for field, value in facts.items():
                confirmed[field] = {"value": value, "source": "verified_evidence", "evidence_id": evidence_id,
                                    "attachment_id": attachment_id, "sha256": sha256, "message_id": material["message_id"]}
            c["facts"], c["version"] = confirmed, c["version"] + 1
            c["pending_fields"] = pending_fields(c)
            await self._save(conn, c, invalidate="evidence_changed")
            await conn.execute(f"INSERT INTO case_actions VALUES($1,$2,'evidence',$3::jsonb,{_NOW})",
                               key, case_id, dumps({**payload, "result_version": c["version"]}))
            await self._event(conn, c, "evidence_adopted", actor.actor_id, {"evidence_id": evidence_id})
            return c["version"]

    async def run(self, run_id: str) -> RunRecord | None:
        row = await self.db.fetchrow(f"SELECT {_COLUMNS} FROM agent_runs WHERE run_id=$1 AND instance=$2",
                                     run_id, self.run_instance)
        return _record(row) if row else None

    async def completed_result(self, run_id: str):
        row = await self.db.fetchrow(
            "SELECT r.result FROM case_run_receipts r JOIN ticket_case_state c ON c.case_id=r.case_id"
            " WHERE r.run_id=$1 AND c.instance=$2", run_id, self.instance)
        return decode(row["result"]) if row else None

    async def complete(self, run: RunRecord, result: dict[str, Any], *, dead=False):
        """Atomic Case effects + run terminal state. Always called with a worker lease."""
        ctx = run.case_context
        material_access_error = False
        sources = {m["attachment_id"]: m for m in ctx["snapshot"].get("review_materials", [])}
        for fact in ctx["snapshot"].get("facts", {}).values():
            if fact.get("source") == "verified_image":
                sources[fact["attachment_id"]] = fact
        # Slow storage runs BEFORE locking agent_runs so the worker's heartbeat
        # can extend its lease. The hash identifies the bytes we actually read;
        # PostgreSQL cannot lock an external object's future contents.
        if self.attachments:
            try:
                actor = await self.attachments.actors.from_snapshot(ctx)
                for material in sources.values():
                    _, row = await self.attachments.content(actor, material["attachment_id"])
                    if row["sha256"] != material["sha256"]:
                        raise conflict("attachment_changed")
                    await self.attachments.content(actor, material["attachment_id"], preview=True)
            except HTTPException:
                material_access_error = True
        async with self.db.transaction() as conn:
            r = await conn.fetchrow(
                f"SELECT *,lease_until>{_NOW} AS live FROM agent_runs WHERE run_id=$1 AND instance=$2 FOR UPDATE",
                run.run_id, self.run_instance)
            prior = await conn.fetchrow("SELECT * FROM case_run_receipts WHERE run_id=$1", run.run_id)
            if prior:
                return decode(prior["result"]), bool(prior["applied"])
            if not r or r["status"] != "running" or r["lease_owner"] != run.lease_owner or r["attempt"] != run.attempt or not r["live"]:
                raise conflict("run_lease_lost")
            c = await self._locked(conn, ctx["case_id"])
            reason = ""
            if self.actors:
                try:
                    actor = await self.actors.from_snapshot(ctx, conn=conn)
                    actor.require_policy(decode(c.get("policy_scope"), {}))
                except HTTPException:
                    reason = "actor_access_changed"
            if c["version"] != ctx["expected_case_version"]:
                reason = "case_version_changed"
            elif c["control_version"] != ctx["expected_control_version"]:
                reason = "case_control_changed"
            elif c["status"] not in ACTIVE:
                reason = "case_closed"
            # Human-owned inputs may be recorded, but can never publish an AI draft.
            elif c["control_mode"] == "human":
                reason = "human_control"
            elif result.get("actor_access_changed"):
                reason = "actor_access_changed"
            elif result.get("service_unavailable"):
                reason = "service_unavailable"
            if not reason and self.attachments:
                try:
                    actor = await self.attachments.actors.from_snapshot(ctx, conn=conn)
                    for material in sorted(sources.values(), key=lambda m: m["attachment_id"]):
                        row = await self.attachments.get(actor, material["attachment_id"], conn=conn)
                        if row["sha256"] != material["sha256"]:
                            raise conflict("attachment_changed")
                        if not await conn.fetchval("SELECT 1 FROM case_materials WHERE case_id=$1 AND attachment_id=$2 AND sha256=$3",
                                                   c["case_id"], material["attachment_id"], material["sha256"]):
                            raise conflict("attachment_changed")
                except HTTPException:
                    reason = "material_access_changed"
            refs = list(result.get("knowledge_sources") or [])
            business = result.get("business_data") or {}
            check_evidence = refs + ([{"source": "business_query", **business}] if business else [])
            check_evidence += [f for f in ctx["snapshot"].get("facts", {}).values() if f.get("source") == "verified_image"]
            if not reason and not await self._evidence_valid(check_evidence, conn=conn):
                reason = "draft_evidence_stale"
            if result.get("material_access_changed") or material_access_error:
                reason = "material_access_changed"
            if not reason and not dead:
                from app.tickets.draft_decision import reconcile_handoff
                result = reconcile_handoff(result, ctx)
            payload = {**{k: v for k, v in result.items() if k != "material_reviews"}, "case_id": c["case_id"], "ticket_id": c["case_id"],
                       "turn_id": r["turn_id"],
                       "case_version": c["version"], "control_version": c["control_version"],
                       "run": {"run_id": run.run_id, "attempt": run.attempt}, "case_applied": not reason}
            if reason:
                CASE_REJECTIONS.labels(reason=reason).inc()
                response = ("此问题已由人工接管，材料已保存。" if reason == "human_control" else
                            "服务暂时不可用，材料已保存。" if reason == "service_unavailable" else
                            "材料或处理权限已变化，本次旧结果已作废。请查看最新工单。")
                payload.update(response=response,
                               case_rejection=reason, handoff=False, draft_id=None,
                               knowledge_sources=[], business_data={}, knowledge={})
                await self._event(conn, c, "result_rejected", data={"run_id": run.run_id, "reason": reason})
            elif "material_reviews" in result:
                await self._apply_reviews(conn, c, run, result["material_reviews"], payload)
            else:
                pending = decode(c["pending_fields"], [])
                c["status"] = "waiting_for_info" if pending or result.get("needs_clarification") else "in_progress"
                # These transitions invalidate earlier approvals; the new draft binds the committed version.
                c["version"] += 1
                await self._save(conn, c, invalidate="new_result")
                payload.update(case_version=c["version"], case_status=c["status"])
                if not dead and not result.get("service_unavailable"):
                    draft_id = "dr_" + run.run_id
                    evidence = [{"field": field, **value} for field, value in decode(c["facts"], {}).items()]
                    from app.core.business_facts import rendered_images
                    image_refs = {field: f"I{i}" for i, (field, _) in enumerate(rendered_images(decode(c["facts"], {})), 1)}
                    for item in evidence:
                        if item["field"] in image_refs:
                            item["reply_reference"] = image_refs[item["field"]]
                    evidence += list(result.get("knowledge_sources") or [])
                    if result.get("business_data"):
                        evidence.append({"source": "business_query", **result["business_data"]})
                    evidence += [{"source": "tool_trace", "trace": entry} for entry in result.get("trajectory", [])]
                    await conn.execute(
                        f"INSERT INTO case_drafts(id,case_id,run_id,version,control_version,body,body_hash,evidence,evidence_version,created_at)"
                        f" VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,1,{_NOW})",
                        draft_id, c["case_id"], run.run_id, c["version"], c["control_version"], payload["response"],
                        hashlib.sha256(payload["response"].encode()).hexdigest(), dumps(evidence))
                    payload["draft_id"] = draft_id
                if result.get("handoff") or dead:
                    await self._handoff(conn, c, payload)
                await self._event(conn, c, "result_applied", data={"run_id": run.run_id, "draft_id": payload.get("draft_id")})
            await conn.execute(
                f"INSERT INTO case_run_receipts VALUES($1,$2,$3,$4,$5::jsonb,{_NOW})",
                run.run_id, c["case_id"], not reason, reason, dumps(payload))
            # Assistant history is authoritative and atomic too; no background audit on this path.
            seq = await conn.fetchval(
                f"INSERT INTO conversation_log(conv_id,user_id,request_id,role,content,agent_type,created_at)"
                f" VALUES($1,$2,$3,'assistant',$4,$5,{_NOW}) RETURNING seq",
                run.conv_id, run.user_id, run.request_id, payload["response"], payload.get("agent_type", "system"))
            await conn.execute("INSERT INTO conversation_log_turns(seq,turn_id,instance) VALUES($1,$2,$3)",
                               seq, r["turn_id"], self.run_instance)
            await conn.execute(
                "INSERT INTO case_messages VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                "reply_" + run.run_id, c["case_id"], self.instance, run.conv_id, run.user_id, seq,
                run.run_id, c["version"], hashlib.sha256(payload["response"].encode()).hexdigest())
            status = "dead" if dead else "succeeded"
            finished = await conn.fetchval(
                f"UPDATE agent_runs SET status=$2,result=$3::jsonb,error=$4,finished_at={_NOW},updated_at={_NOW},"
                f"lease_owner='',lease_until=0,history=jsonb_set(history,ARRAY[(jsonb_array_length(history)-1)::text],"
                f"(history->-1)||jsonb_build_object('ended_at',{_NOW},'outcome',$2::text)) WHERE run_id=$1"
                f" AND lease_owner=$5 AND attempt=$6 AND status='running' AND lease_until>{_NOW} RETURNING run_id",
                run.run_id, status, dumps(payload), reason, run.lease_owner, run.attempt)
            if finished is None:
                # The lease may have expired while waiting for the parent Case
                # lock. Roll back ALL draft/history/outbox changes in that case.
                raise conflict("run_lease_lost")
        return payload, not reason

    async def _apply_reviews(self, conn, c, run, reviews, payload):
        """Called ONLY inside complete's Case+lease transaction; no external effects."""
        facts, pending = decode(c["facts"], {}), set(decode(c["pending_fields"], []))
        pending.discard("image:review")
        by_field = {}
        for review in reviews:
            for item in review["fields"]:
                if item["outcome"] == "matched" and not fresh((item.get("source") or {}).get("observed_at")):
                    item["outcome"] = "unverified"
            if any(i["field"] in {"order_ref", "model", "expected_model", "received_model"}
                   and i["outcome"] == "matched" and facts.get(i["field"])
                   and facts[i["field"]]["value"] != i["db_value"] for i in review["fields"]):
                # A different pictured model cannot lend its quantity to the
                # Case's existing model, even when both are real DB records.
                for item in review["fields"]:
                    if item["outcome"] == "matched":
                        item["outcome"] = "case_conflict"
            for item in review["fields"]:
                by_field.setdefault(item["field"], []).append((review, item))
        if any(not review["fields"] for review in reviews):
            # One readable image cannot clear another image's failed/empty
            # extraction in this batch. A later fully readable supplement can.
            pending.add("image:readability")
        elif by_field:
            pending.discard("image:readability")
        for field, items in by_field.items():
            values = {i["db_value"] for _, i in items if i["outcome"] == "matched"}
            confirmed = len(values) == 1 and all(i["outcome"] == "matched" for _, i in items)
            value = next(iter(values)) if confirmed else None
            old = facts.get(field)
            if old and old.get("value") != value:
                confirmed = False  # never overwrite an employee/prior confirmed fact silently
            if confirmed:
                review, item = items[0]
                facts[field] = {"value": value, "db_value": value, "candidate_value": item["value"],
                                "source": "verified_image", "unit": item["unit"],
                                "attachment_id": review["attachment_id"], "sha256": review["sha256"],
                                "evidence_id": run.run_id + ":" + field, "business_source": item["source"],
                                "message_id": run.case_context["message_id"]}
                pending.discard("image:" + field)
            else:
                pending.add("image:" + field)
                for _, item in items:
                    if item["outcome"] == "matched":
                        item["outcome"] = "case_conflict"
        c.update(facts=facts, pending_fields=sorted(pending), version=c["version"] + 1)
        c["pending_fields"] = pending_fields(c)
        c["status"] = "waiting_for_info" if c["pending_fields"] else "in_progress"
        await conn.execute("SELECT set_config('ai_helpdesk.image_review','on',true)")
        await self._save(conn, c, invalidate="image_review")
        await conn.execute("SELECT set_config('ai_helpdesk.image_review','off',true)")
        for review in reviews:
            await conn.execute(f"INSERT INTO case_image_reviews VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,{_NOW})",
                               c["case_id"], review["attachment_id"], review["sha256"], run.run_id,
                               c["version"], c["control_version"], dumps(review))
        # New stable message/run identity. Insertion and source terminal receipt
        # share the transaction, so a crash cannot strand the continuation.
        message_id, next_run = "verified_" + run.run_id, new_run_id()
        snapshot = {**run.case_context["snapshot"], "facts": facts, "pending_fields": c["pending_fields"],
                    "status": c["status"], "review_materials": []}
        from app.tickets.draft_decision import complete_order_image_review
        context = {**run.case_context, "message_id": message_id, "expected_case_version": c["version"],
                   "snapshot": snapshot, "materials_reviewed": True,
                   "image_review_complete": complete_order_image_review(reviews)}
        content = "图片核对已完成；请按已确认事实和待补字段继续处理。"
        seq = await conn.fetchval(f"INSERT INTO conversation_log(conv_id,user_id,role,content,created_at)"
                                  f" VALUES($1,$2,'system',$3,{_NOW}) RETURNING seq", run.conv_id, run.user_id, content)
        await conn.execute("INSERT INTO conversation_log_turns(seq,instance) VALUES($1,$2)", seq, self.run_instance)
        await conn.execute("INSERT INTO case_messages VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)", message_id,
                           c["case_id"], self.instance, run.conv_id, run.user_id, seq, next_run, c["version"],
                           hashlib.sha256(content.encode()).hexdigest())
        await conn.execute(f"INSERT INTO agent_runs(run_id,instance,idempotency_key,conv_id,user_id,message,request_id,"
                           f"transport,case_context,created_at,updated_at,dispatched_at)"
                           f" VALUES($1,$2,$3,$4,$5,$6,$7,'case',$8::jsonb,{_NOW},{_NOW},0)",
                           next_run, self.run_instance, message_id, run.conv_id, run.user_id, content,
                           run.request_id, dumps(context))
        payload.update(response="图片核对结果已保存，正在继续处理。", case_version=c["version"],
                       case_status=c["status"], next_run_id=next_run, draft_id=None, handoff=False)
        await self._event(conn, c, "images_reviewed", data={"run_id": run.run_id, "next_run_id": next_run})

    async def _handoff(self, conn, c, payload):
        key = f"handoff:{c['case_id']}"
        created = await conn.fetchval(
            f"INSERT INTO case_actions VALUES($1,$2,'handoff',$3::jsonb,{_NOW})"
            " ON CONFLICT DO NOTHING RETURNING action_key", key, c["case_id"], dumps({"reason": payload.get("handoff_reason", "")}))
        CASE_ACTIONS.labels(outcome="created" if created else "deduplicated").inc()
        if self.webhook_url:
            ticket = dict(await conn.fetchrow("SELECT * FROM tickets WHERE id=$1", c["case_id"]))
            await conn.execute(
                f"INSERT INTO ticket_notifications(id,ticket_id,url,payload,next_attempt_at,created_at,updated_at)"
                f" VALUES($1,$2,$3,$4,{_NOW},{_NOW},{_NOW}) ON CONFLICT(ticket_id) DO NOTHING",
                "nt_" + hashlib.sha256(key.encode()).hexdigest()[:24], c["case_id"], self.webhook_url,
                dumps({"type": "ticket.created", "ticket": ticket}))
        payload["ticket_notification"] = "queued" if self.webhook_url else "not_configured"
