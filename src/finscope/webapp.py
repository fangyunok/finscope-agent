"""Local FinScope workbench with signed demo identity and explicit memory writes."""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from .agent import AgentRunner, TermQA
from .service import DomainError, FinanceService

_COOKIE = "finscope_demo"
_SESSION_SECONDS = 8 * 60 * 60
_MAX_BODY = 32768


class _HTTPProblem(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code, self.message, self.status_code = code, message, status_code


class SameOriginMiddleware:
    """Reject cross-origin browser writes; permit local non-browser clients."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] in {"POST", "PATCH", "PUT", "DELETE"}:
            request = Request(scope)
            allowed = request.headers.get("sec-fetch-site", "").lower() not in {"cross-site", "same-site"}
            origin = request.headers.get("origin")
            if origin is not None:
                try:
                    source, target = urlsplit(origin), urlsplit(str(request.url))
                    def port(value):
                        return value.port or (443 if value.scheme == "https" else 80)
                    allowed = allowed and source.scheme in {"http", "https"} and not source.username and not source.password
                    allowed = allowed and not source.path and not source.query and not source.fragment
                    allowed = allowed and (source.scheme, source.hostname, port(source)) == (target.scheme, target.hostname, port(target))
                except ValueError:
                    allowed = False
            if not allowed:
                await JSONResponse({"error": "origin_rejected", "message": "请从本机工作台提交请求。"}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class _SessionSigner:
    def __init__(self, secret: bytes):
        self.secret = secret

    def sign(self, user_id: str) -> str:
        payload = base64.urlsafe_b64encode(json.dumps({"user_id": user_id, "expires": int(time.time()) + _SESSION_SECONDS}, separators=(",", ":")).encode()).decode().rstrip("=")
        signature = hmac.new(self.secret, payload.encode("ascii"), hashlib.sha256).hexdigest()
        return payload + "." + signature

    def read(self, token: str | None) -> str:
        if not token or len(token) > 2048:
            raise _HTTPProblem("authentication_required", "请先选择模拟用户。", 401)
        try:
            payload, supplied = token.split(".", 1)
            if not hmac.compare_digest(supplied, hmac.new(self.secret, payload.encode("ascii"), hashlib.sha256).hexdigest()):
                raise ValueError("signature")
            decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if not isinstance(decoded, dict) or not isinstance(decoded.get("user_id"), str):
                raise ValueError("payload")
            expires = decoded.get("expires")
            if type(expires) is not int or expires <= time.time():
                raise ValueError("expiry")
            return decoded["user_id"]
        except (ValueError, UnicodeError, TypeError):
            raise _HTTPProblem("authentication_required", "模拟会话已失效，请重新选择用户。", 401) from None


async def _json_body(request: Request, *, optional: bool = False) -> dict[str, Any]:
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) < 0 or int(length) > _MAX_BODY:
                raise _HTTPProblem("body_too_large", "请求内容过大。", 413)
        except ValueError:
            raise _HTTPProblem("invalid_body", "请求长度不合法。") from None
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > _MAX_BODY:
            raise _HTTPProblem("body_too_large", "请求内容过大。", 413)
    if optional and not received:
        return {}
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise _HTTPProblem("unsupported_media_type", "请使用 JSON 请求。", 415)
    try:
        value = json.loads(received)
    except (ValueError, UnicodeError):
        raise _HTTPProblem("invalid_json", "JSON 格式不正确。") from None
    if not isinstance(value, dict):
        raise _HTTPProblem("invalid_body", "请求必须为 JSON 对象。")
    return value


def _fields(payload: dict, allowed: set[str], required: set[str] | None = None):
    if set(payload) - allowed:
        raise _HTTPProblem("unexpected_fields", "请求包含不支持的字段；身份由服务端会话确定。")
    if required and required - set(payload):
        raise _HTTPProblem("missing_fields", "请求缺少必要字段。")


def _version(payload: dict) -> int:
    value = payload.get("expected_version")
    if type(value) is not int or value < 0:
        raise _HTTPProblem("invalid_version", "请提供当前画像版本。")
    return value


def _string(payload: dict, key: str, maximum: int = 4000) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise _HTTPProblem("invalid_field", "字段 " + key + " 不能为空或超过长度限制。")
    return value.strip()


def _confirmed(payload: dict):
    if payload.get("confirmed") is not True:
        raise _HTTPProblem("confirmation_required", "此操作需要用户明确确认。")


def _encode(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    raise TypeError(type(value).__name__)


def _response(value, status_code=200):
    normalized = json.loads(json.dumps(value, ensure_ascii=False, default=_encode))
    return JSONResponse(normalized, status_code=status_code, headers={"cache-control": "no-store", "x-content-type-options": "nosniff"})


_HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>FinScope · 用户画像与金融需求分析平台</title><style>
:root{--bg:#f4f6f9;--ink:#192e43;--muted:#718093;--line:#e3e9ee;--navy:#142e3e;--teal:#087f7f;--light:#e7f4f1;--amber:#92621e}*{box-sizing:border-box}body{margin:0;font:14px/1.65 system-ui,'Microsoft YaHei',sans-serif;background:var(--bg);color:var(--ink)}button,input,select,textarea{font:inherit}button{cursor:pointer;border:0;border-radius:7px;padding:10px 15px;background:var(--teal);color:white;font-weight:600}button:disabled{opacity:.5;cursor:wait}.secondary{background:#f0f4f7;color:#476174;border:1px solid #d6e0e7}.danger{background:#fff1ee;color:#a1473a;border:1px solid #f0c9c1}.small{font-size:12px;padding:6px 10px}.shell{min-height:100vh;display:grid;grid-template-columns:210px 1fr}.sidebar{background:var(--navy);color:#bdd1dc;padding:30px 24px;display:flex;flex-direction:column}.brand{font-size:25px;font-weight:750;color:white;letter-spacing:-.8px}.brand span{color:#64d0be}.subtitle{font-size:11px;color:#8dacbc;margin-top:6px}.nav{margin-top:35px;background:#244757;color:#e9f7f6;border-radius:8px;padding:12px}.sidebar p{font-size:12px;color:#92acbd;line-height:2;margin-top:auto;padding-top:22px;border-top:1px solid #365463}.main{padding:27px 32px 38px;max-width:1500px;width:100%;margin:auto}.topbar,.row{display:flex;align-items:center;justify-content:space-between;gap:12px}.topbar{margin-bottom:22px}.eyebrow{font-size:11px;letter-spacing:1.2px;font-weight:700;color:var(--teal)}h1{font-size:28px;letter-spacing:-.7px;margin:3px 0}h2{font-size:17px;margin:0 0 15px}h3{font-size:14px;margin:0 0 8px}.muted{font-size:12px;color:var(--muted)}.identity{display:flex;gap:8px;align-items:center}.identity select{width:205px;font-size:12px}.notice{background:#fff8e9;border:1px solid #ecdfbd;color:#805e2b;border-radius:9px;padding:10px 14px;font-size:12px;margin-bottom:21px}.grid{display:grid;grid-template-columns:minmax(330px,.9fr) minmax(420px,1.3fr);gap:20px;align-items:start}.card{background:white;border:1px solid var(--line);border-radius:12px;padding:22px;margin-bottom:20px;box-shadow:0 5px 18px #203f4e04}.badge{font-size:11px;border:1px solid #c8e5dc;background:var(--light);color:#187665;padding:4px 8px;border-radius:16px;white-space:nowrap}.badge.grey{color:#697b8e;background:#f1f4f7;border-color:#dce3ea}.badge.warn{background:#fff6e7;color:var(--amber);border-color:#eeddbb}.fields{display:grid;grid-template-columns:1fr 1fr;gap:10px 14px}label{display:block;font-size:12px;font-weight:600;color:#506679;margin:7px 0 5px}input,select,textarea{width:100%;border:1px solid #cdd9e1;border-radius:7px;padding:9px 11px;color:var(--ink);background:white}input:focus,select:focus,textarea:focus{outline:3px solid #087f7f15;border-color:var(--teal)}textarea{min-height:92px;resize:vertical}.caption{font-size:11px;color:var(--muted);margin:9px 0}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:15px}.empty{font-size:12px;color:var(--muted);border:1px dashed #d6dfe6;border-radius:8px;text-align:center;padding:23px 12px}.feedback{display:none;white-space:pre-wrap;font-size:12px;border-radius:8px;padding:11px 14px;color:#1b7560;background:#e9f6ef;margin-bottom:18px}.feedback.error{background:#fff0ee;color:#a7483c}.hide{display:none!important}.check{display:flex;gap:8px;align-items:flex-start;font-weight:400;margin-top:14px;font-size:12px}.check input{width:15px;flex:none;margin-top:4px}.divider{border-top:1px solid var(--line);margin-top:17px;padding-top:17px}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:15px 0}.stat{padding:13px 11px;border-radius:8px;background:#f3f6f9}.stat strong{display:block;font-size:23px;letter-spacing:-.5px;font-variant-numeric:tabular-nums}.stat span{font-size:11px;color:var(--muted)}.stat.teal{background:#eaf6f2;color:#197662}.stat.amber{background:#fff6e7;color:var(--amber)}.result-copy{font-size:13px;line-height:1.9}.product{padding:14px 0;border-top:1px solid var(--line)}.product:first-child{border-top:0}.product-name{font-weight:650;color:#234253}.score{font-size:12px;color:var(--teal);font-variant-numeric:tabular-nums}.chips{font-size:11px;display:flex;gap:5px;flex-wrap:wrap;margin:8px 0}.chip{background:#f1f5f7;color:#627786;padding:3px 7px;border-radius:4px}.reasons{font-size:12px;color:#586f81;margin:6px 0}.source{font-size:12px;border-left:3px solid #89c8ba;background:#f4f8f8;padding:10px 12px;margin:10px 0}.source strong{display:block;font-size:11px;margin-bottom:4px}.source p{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}.diff{width:100%;border-collapse:collapse;font-size:12px}.diff td,.diff th{padding:8px 6px;border-bottom:1px solid var(--line);text-align:left}.diff th{font-weight:600;color:#668094}.history-button{background:white;color:var(--ink);display:block;width:100%;padding:11px 0;border-bottom:1px solid var(--line);border-radius:0;text-align:left;font-weight:400;font-size:12px}.history-button span{display:block;font-size:11px;color:var(--muted)}details{margin-top:12px}summary{cursor:pointer;font-size:12px;color:var(--muted)}pre{max-height:360px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font:11px/1.6 ui-monospace,monospace;background:#f3f6f9;border-radius:8px;padding:12px}.trace{max-height:220px;overflow:auto}.footer{font-size:11px;color:#7e92a1;margin-top:24px}a{color:var(--teal)}.scroll{max-height:260px;overflow:auto}.statusline{font-size:11px;color:var(--muted);margin-top:5px}
@media(max-width:1100px){.shell{grid-template-columns:170px 1fr}.sidebar{padding:25px 18px}.main{padding:24px}.grid{grid-template-columns:1fr}.topbar{align-items:flex-start;flex-direction:column}}@media(max-width:650px){.shell{display:block}.sidebar{padding:16px 20px}.nav,.sidebar p,.subtitle{display:none}.brand{font-size:22px}.main{padding:20px 14px}.card{padding:18px}.identity{flex-wrap:wrap}.fields{grid-template-columns:1fr}.stat strong{font-size:18px}h1{font-size:25px}}
</style></head><body><div class="shell"><aside class="sidebar"><div class="brand">Fin<span>Scope</span></div><div class="subtitle">用户画像与金融需求分析平台</div><div class="nav">◈ &nbsp; 需求分析工作台</div><p>确认的画像与记忆<br>确定性财务计算<br>约束过滤 · 可解释排序<br>有效条款与来源核对<br>模拟目录 · 不接入资金</p></aside><main class="main">
<header class="topbar"><div><div class="eyebrow">PROFILE & FINANCIAL NEEDS</div><h1>需求分析工作台</h1><div class="muted">让每一次分析都有确认的输入、明确的假设与条款依据。</div></div><div class="identity"><select id="users" aria-label="模拟用户"></select><button id="login" class="small">切换用户</button><button id="logout" class="secondary small hide">退出</button></div></header>
<div class="notice">演示环境：用户、产品、费用与条款均为虚构数据。输出用于展示需求分析与工程流程。当前模式：<strong>__MODE_LABEL__</strong>。</div><div id="feedback" class="feedback" role="status"></div>
<div class="grid"><section><div class="card"><div class="row"><h2>已确认画像</h2><span id="profile-version" class="badge grey">尚未选择用户</span></div><div id="identity-status" class="muted">请先选择一个模拟用户。</div><div class="fields">
<div><label for="monthly_income">月收入 · 元</label><input id="monthly_income" inputmode="decimal" placeholder="8000.00"></div><div><label for="essential_expense">必要月支出 · 元</label><input id="essential_expense" inputmode="decimal" placeholder="6500.00"></div><div><label for="available_balance">可用余额 · 元</label><input id="available_balance" inputmode="decimal" placeholder="5000.00"></div><div><label for="goal_amount">目标金额 · 元</label><input id="goal_amount" inputmode="decimal" placeholder="12000.00"></div><div><label for="horizon_months">目标期限 · 月</label><input id="horizon_months" type="number" min="1" max="1200" placeholder="6"></div><div><label for="goal_kind">目标类型</label><select id="goal_kind"><option value="">待确认</option><option value="emergency">应急储备</option><option value="purchase">计划购买</option><option value="long_term">长期目标</option></select></div><div><label for="liquidity_need">流动性需求</label><select id="liquidity_need"><option value="">待确认</option><option value="immediate">随时取用</option><option value="flexible">允许短期等待</option><option value="locked">允许锁定</option></select></div><div><label for="risk_preference">本人确认的风险偏好</label><select id="risk_preference"><option value="">待确认</option><option value="low">低</option><option value="medium">中</option><option value="high">高</option></select></div></div>
<p class="caption">改动先生成待确认提议。收藏与浏览不会改变风险偏好。</p><div class="actions"><button id="demo-fill" class="secondary">填入模拟资料</button><button id="propose" class="secondary">核对画像变更</button><button id="refresh" class="secondary">刷新已确认资料</button></div><details><summary>查看字段来源</summary><pre id="field-sources">尚无资料。</pre></details>
<div id="proposal" class="hide divider"><div class="row"><h3>待确认变更</h3><span id="proposal-version" class="badge warn"></span></div><div id="proposal-diff"></div><label class="check"><input id="confirm-check" type="checkbox"><span>我已核对这些字段，同意以本次内容更新长期画像。</span></label><button id="confirm" class="small">确认此版本变更</button></div></div>
<div class="card"><h2>条款问答</h2><div class="fields"><div><label for="term-question">问题</label><input id="term-question" value="取用费用" maxlength="500"></div><div><label for="as-of">条款日期</label><input id="as-of" type="date" value="2026-10-03"></div></div><div class="actions"><button id="term-answer" class="secondary">检索并核对条款</button></div><div id="term-result" class="caption">仅引用指定日期有效的模拟产品条款。</div></div>
<div class="card"><h2>记忆与隐私</h2><p class="caption">删除将移除本人的画像、字段来源、提议、显式反馈和分析记录。下次分析需要重新确认输入。</p><label class="check"><input id="delete-check" type="checkbox"><span>确认删除本人的全部记忆和分析记录。</span></label><button id="delete-memory" class="danger small">删除本人记忆</button></div></section>
<section><div class="card"><div class="row"><h2>发起需求分析</h2><span class="badge">模拟产品目录</span></div><textarea id="message" aria-label="分析需求">请根据我已确认的画像分析目标和匹配方案。</textarea><div class="actions"><button id="analyze">使用当前画像分析</button></div><p class="caption">离线模式读取已确认画像；输入显式 JSON 可生成待确认提议。模型模式只抽取字段，不决定金额、风险或排序。</p></div>
<div class="card"><div class="row"><h2>目标计算与需求匹配</h2><span id="run-status" class="badge grey">尚未分析</span></div><div id="analysis" class="empty">确认画像后开始分析。</div></div>
<div class="card"><div class="row"><h2>最近分析</h2><span class="badge grey">跨会话持久保存</span></div><div id="history" class="empty">选择用户后加载本人记录。</div></div>
</section></div><div class="footer">FinScope · 用户确认的跨会话 Memory / MCP 工具边界 / 确定性计算 / 可解释匹配。此页面使用模拟身份切换，不是生产登录系统。</div></main></div>
<script>
const $=id=>document.getElementById(id),fields=['monthly_income','essential_expense','available_balance','goal_amount','horizon_months','goal_kind','liquidity_need','risk_preference'];let me=null,profile=null,proposal=null,currentRun=null;const names={monthly_income:'月收入',essential_expense:'必要月支出',available_balance:'可用余额',goal_amount:'目标金额',horizon_months:'目标期限',liquidity_need:'流动性需求',risk_preference:'风险偏好',goal_kind:'目标类型',currency:'货币'};const labels={immediate:'随时取用',flexible:'允许短期等待',locked:'允许锁定',low:'低',medium:'中',high:'高',emergency:'应急储备',purchase:'计划购买',long_term:'长期目标',completed:'已完成',pending_confirmation:'待确认',needs_profile:'待补充',failed:'失败',running:'执行中'};const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const list=value=>Array.isArray(value)?value:(value?.items||value?.runs||value?.products||value?.candidates||[]);const money=value=>value===undefined||value===null?'—':'¥'+Number(value).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});const display=value=>value===undefined||value===null?'未确认':(labels[value]||String(value));
async function api(path,options={}){const response=await fetch(path,{...options,credentials:'same-origin',headers:{'content-type':'application/json',...(options.headers||{})},body:options.body===undefined?undefined:JSON.stringify(options.body)});const value=await response.json();if(!response.ok)throw Error(value.message||value.error||'请求失败');return value;}function feedback(message,error=false){$('feedback').textContent=message;$('feedback').className='feedback'+(error?' error':'');$('feedback').style.display='block';}function requireLogin(){if(!me)throw Error('请先选择模拟用户。');}function guarded(id,handler){$(id).addEventListener('click',async()=>{const button=$(id);button.disabled=true;try{await handler();}catch(error){feedback(error.message,true);}finally{button.disabled=false;}});}function purgeViews(){profile=null;proposal=null;currentRun=null;fields.forEach(key=>$(key).value='');$('proposal').classList.add('hide');$('confirm-check').checked=false;$('delete-check').checked=false;$('analysis').className='empty';$('analysis').textContent='尚无当前分析。';$('run-status').textContent='尚未分析';$('field-sources').textContent='暂无已确认字段。';$('term-result').textContent='仅引用指定日期有效的模拟产品条款。';$('history').className='empty';$('history').textContent='暂无分析记录。';$('message').value='请根据我已确认的画像分析目标和匹配方案。';}
function renderProfile(value){profile=value;const data=value.data||{};fields.forEach(key=>$(key).value=data[key]??'');$('profile-version').textContent='v'+value.version+' · 记忆代次 '+value.memory_epoch;$('identity-status').textContent=(me?.display_name||'')+' · '+(me?.tenant_id||'');$('field-sources').textContent=JSON.stringify(value.field_sources||{},null,2);}
function renderProposal(value){proposal=value;$('proposal').classList.remove('hide');$('confirm-check').checked=false;$('proposal-version').textContent='基于 v'+value.base_version;const changes=value.changes||{};let diffs=value.diff;if(!Array.isArray(diffs))diffs=Object.entries(changes).map(([field,next])=>({field,old:profile?.data?.[field],new:next}));$('proposal-diff').innerHTML='<table class="diff"><thead><tr><th>字段</th><th>当前</th><th>本次</th></tr></thead><tbody>'+diffs.map(row=>'<tr><td>'+esc(names[row.field]||row.field)+'</td><td>'+esc(display(row.old??row.before??row.old_value))+'</td><td>'+esc(display(row.new??row.after??row.new_value))+'</td></tr>').join('')+'</tbody></table>';}
function sourcesHtml(sources){return list(sources).map(source=>'<div class="source"><strong>'+esc(source.product_name||source.product_id||'模拟条款')+' · '+esc(source.term_id||'')+' · v'+esc(source.version||'')+' · '+esc(source.effective_from||'')+'</strong><p>'+esc(source.content||'')+'</p></div>').join('');}
function productHtml(product,excluded=false){const reasons=product.reasons||product.exclusion_reasons||product.match_reasons||(product.reason?[product.reason]:[]),score=product.score??product.ranking_score;const reasonsText=Array.isArray(reasons)?reasons.map(reason=>typeof reason==='string'?reason:(reason.message||reason.reason||JSON.stringify(reason))).join('；'):JSON.stringify(reasons);return '<div class="product"><div class="row"><span class="product-name">'+esc(product.name||product.product_name||product.product_id)+'</span><span class="score">'+(excluded?'不满足约束':score===undefined?'已满足约束':'规则评分 '+esc(score))+'</span></div><div class="chips"><span class="chip">'+esc(product.product_id||'')+'</span><span class="chip">'+esc(display(product.risk_label||product.risk_level))+'</span><span class="chip">模拟产品</span></div><div class="reasons">'+esc(reasonsText)+'</div>'+(excluded?'':'<div class="actions"><button class="small secondary product-feedback" data-product="'+esc(product.product_id)+'" data-action="favorite">确认收藏</button><button class="small secondary product-feedback" data-product="'+esc(product.product_id)+'" data-action="exclude">确认排除</button><button class="small secondary product-feedback" data-product="'+esc(product.product_id)+'" data-action="clear">清除反馈</button></div>')+'</div>';}
function renderAnalysis(record){currentRun=record;const result=record.result||record;$('analysis').className='';$('run-status').textContent=labels[result.status||record.status]||result.status||record.status||'';if(result.proposal){renderProposal(result.proposal);$('analysis').innerHTML='<div class="result-copy">'+esc(result.summary||'请确认画像变更后重新分析。')+'</div>';return;}if(result.error){$('analysis').innerHTML='<div class="result-copy">'+esc(result.error.message||result.error.code)+'</div>';return;}if(result.status==='needs_profile'){$('analysis').innerHTML='<div class="result-copy">请先补充并确认画像：'+esc((result.missing_fields||[]).map(key=>names[key]||key).join('、'))+'</div><p class="caption">资料不足时不猜测收入、风险偏好或产品匹配结果。</p>';return;}const calc=result.calculation||{},match=result.matching||{},out=calc.result||calc;let html='<div class="result-copy">'+esc(result.summary||'已按当前确认画像完成分析。')+'</div><div class="statusline">画像 v'+esc(result.profile_version??record.profile_version)+' · '+(result.model_used?'模型字段抽取':'离线确定性流程，未调用大模型')+'</div>';const surplus=out.monthly_surplus??out.monthly_surplus_cny??out.surplus, gap=out.goal_gap??out.remaining_goal??out.gap??out.gap_cny,months=out.months_to_goal??out.months_needed??out.minimum_months;html+='<div class="stats"><div class="stat teal"><strong>'+money(surplus)+'</strong><span>月结余 · 元</span></div><div class="stat"><strong>'+money(gap)+'</strong><span>目标差额 · 元</span></div><div class="stat amber"><strong>'+esc(months===null||months===undefined?'—':months+' 个月')+'</strong><span>忽略利息的最短达成时间</span></div></div>';const horizon=out.horizon_months,feasible=out.horizon_feasible;html+='<p class="result-copy"><strong>目标期限：'+esc(horizon??'未确认')+' 个月 · '+(feasible===true?'当前假设下可按期达到':out.status==='unreachable'?'月结余不足，当前假设下无法达到目标':'当前假设下无法按期达到')+'</strong></p>';const assumptions=calc.assumptions||out.assumptions||[];html+='<p class="caption">'+esc(Array.isArray(assumptions)?assumptions.join('；'):assumptions)+'</p>';if(result.missing_fields?.length)html+='<p class="reasons">待补充：'+esc(result.missing_fields.map(key=>names[key]||key).join('、'))+'</p>';const candidates=match.eligible||match.candidates||match.ranked||match.products||match.matches||[],excluded=match.excluded||match.exclusions||[];html+='<h3 class="divider">满足已确认约束的候选</h3>'+(list(candidates).length?list(candidates).map(p=>productHtml(p)).join(''):'<div class="empty">没有满足当前约束的候选，或画像尚未完整。</div>');if(list(excluded).length)html+='<details><summary>查看 '+list(excluded).length+' 个不匹配产品与原因</summary>'+list(excluded).map(p=>productHtml(p,true)).join('')+'</details>';const sources=result.sources||match.sources||[];html+='<details><summary>查看有效模拟条款</summary>'+sourcesHtml(sources)+'</details><details><summary>查看计算输入、排序依据与 MCP 调用轨迹</summary><pre>'+esc(JSON.stringify({calculation:calc,matching:match,tool_events:result.tool_events,usage:result.usage},null,2))+'</pre></details>';$('analysis').innerHTML=html;$('analysis').querySelectorAll('.product-feedback').forEach(button=>button.addEventListener('click',async()=>{button.disabled=true;try{await api('/api/feedback',{method:'POST',body:{product_id:button.dataset.product,action:button.dataset.action,confirmed:true}});feedback('已更新本人显式反馈，请重新分析查看匹配结果。收藏不会调整风险或评分；确认排除会过滤候选。');}catch(error){feedback(error.message,true);}finally{button.disabled=false;}}));}
async function refreshHistory(){const runs=list(await api('/api/runs'));$('history').className='scroll';$('history').innerHTML=runs.length?runs.map(run=>'<button class="history-button" data-run="'+esc(run.run_id)+'">'+esc(labels[run.status]||run.status)+' · 画像 v'+esc(run.profile_version)+'<span>'+esc(run.run_id)+' · '+esc(run.created_at||'')+'</span></button>').join(''):'<div class="empty">暂无分析记录。</div>';$('history').querySelectorAll('[data-run]').forEach(button=>button.addEventListener('click',async()=>{try{renderAnalysis(await api('/api/runs/'+encodeURIComponent(button.dataset.run)));}catch(error){feedback(error.message,true);}}));}
async function loadIdentity(){me=await api('/api/me');$('users').value=me.user_id;$('logout').classList.remove('hide');renderProfile(await api('/api/profile'));await refreshHistory();const proposals=list(await api('/api/profile-proposals'));const pending=proposals.find(p=>p.status==='pending');if(pending)renderProposal(pending);}
guarded('login',async()=>{await api('/api/login',{method:'POST',body:{user_id:$('users').value}});purgeViews();await loadIdentity();feedback('已切换模拟用户。画像、反馈与分析记录按服务端身份隔离。');});guarded('logout',async()=>{await api('/api/logout',{method:'POST',body:{}});me=null;purgeViews();location.reload();});guarded('refresh',async()=>{requireLogin();purgeViews();await loadIdentity();feedback('已刷新本人已确认资料。');});
guarded('demo-fill',async()=>{requireLogin();const value=await api('/api/demo-profile');fields.forEach(key=>$(key).value=value.data[key]??'');feedback('模拟资料已填入表单，请核对并创建提议、确认后保存。');});guarded('propose',async()=>{requireLogin();if(!profile)throw Error('请先加载当前画像。');const patch={};fields.forEach(key=>{const value=$(key).value.trim();if(value!==''&&(String(profile.data?.[key]??'')!==value))patch[key]=key==='horizon_months'?Number(value):value;});if(!Object.keys(patch).length)throw Error('没有待确认的变更；空白字段不会删除旧值。');renderProposal(await api('/api/profile-proposals',{method:'POST',body:{patch,expected_version:profile.version}}));feedback('请核对待确认提议；当前长期画像尚未更改。');});guarded('confirm',async()=>{requireLogin();if(!proposal||!$('confirm-check').checked)throw Error('请核对并勾选本次变更确认声明。');renderProfile(await api('/api/profile-proposals/'+encodeURIComponent(proposal.proposal_id)+'/confirm',{method:'POST',body:{expected_version:proposal.base_version,expected_hash:proposal.content_hash,confirmed:true}}));proposal=null;currentRun=null;$('proposal').classList.add('hide');$('confirm-check').checked=false;$('analysis').className='empty';$('analysis').textContent='画像已更新至 v'+profile.version+'，请重新分析；历史记录保留各自的画像版本。';$('run-status').textContent='待重新分析';await refreshHistory();feedback('已更新确认画像。下一次分析将使用新版本。');});guarded('analyze',async()=>{requireLogin();renderAnalysis(await api('/api/tasks',{method:'POST',body:{message:$('message').value,request_id:crypto.randomUUID()}}));await refreshHistory();});guarded('term-answer',async()=>{requireLogin();const result=await api('/api/term-answers',{method:'POST',body:{question:$('term-question').value,as_of:$('as-of').value}});$('term-result').innerHTML='<p style="white-space:pre-wrap">'+esc(result.answer||'条款资料不足。')+'</p>'+sourcesHtml(result.sources)+'<p class="caption">'+(result.model_used?'引用结构已核验，语义支持待人工确认。':'直接展示检索条款，未调用大模型。')+'</p>';});
const channel='BroadcastChannel' in window?new BroadcastChannel('finscope-memory-events'):null;if(channel)channel.onmessage=async event=>{if(event.data?.deleted_user===me?.user_id){purgeViews();await loadIdentity();feedback('另一页面已删除本人记忆，当前显示已清空。');}};guarded('delete-memory',async()=>{requireLogin();if(!$('delete-check').checked)throw Error('请先勾选本人记忆删除确认声明。');await api('/api/memory',{method:'DELETE',body:{confirmed:true}});purgeViews();if(channel)channel.postMessage({deleted_user:me.user_id});await loadIdentity();feedback('本人记忆和分析记录已删除。新任务需要重新确认画像。');});
(async()=>{try{const users=list(await api('/api/demo-users'));$('users').innerHTML=users.map(user=>'<option value="'+esc(user.user_id)+'">'+esc(user.display_name)+' · '+esc(user.tenant_id)+'</option>').join('');try{await loadIdentity();}catch{me=null;}}catch(error){feedback(error.message,true);}})();
</script></body></html>'''


def create_app(database_path: Path | str | None = None, model_mode: str = "fixture") -> Starlette:
    if model_mode not in {"fixture", "qwen", "api"}:
        raise ValueError("model_mode must be fixture, qwen, or api")
    database_path = Path(database_path or os.environ.get("FINSCOPE_DB_PATH", "runs/finscope.sqlite"))
    service = FinanceService(database_path)
    service.seed_demo()
    signer = _SessionSigner(secrets.token_bytes(32))

    def principal(request):
        return service.authenticate_demo(signer.read(request.cookies.get(_COOKIE)))

    async def home(request):
        label = {"fixture": "离线确定性流程（不调用大模型）", "qwen": "Qwen 字段抽取与条款问答", "api": "模型 API 字段抽取与条款问答"}[model_mode]
        return HTMLResponse(_HTML.replace("__MODE_LABEL__", html.escape(label)), headers={"cache-control": "no-store", "x-content-type-options": "nosniff", "referrer-policy": "same-origin"})

    async def health(request):
        return _response({"status": "ok", "service": "FinScope", "mode": model_mode, "model_validated": False, "synthetic": True, "demo": True})

    async def users(request):
        return _response(service.list_demo_users())

    async def login(request):
        body = await _json_body(request)
        _fields(body, {"user_id"}, {"user_id"})
        user = service.authenticate_demo(_string(body, "user_id", 128))
        response = _response(user)
        response.set_cookie(_COOKIE, signer.sign(user.user_id), max_age=_SESSION_SECONDS, httponly=True, samesite="strict", secure=request.url.scheme == "https", path="/")
        return response

    async def logout(request):
        principal(request)
        body = await _json_body(request, optional=True)
        _fields(body, set())
        response = _response({"logged_out": True})
        response.delete_cookie(_COOKIE, path="/", httponly=True, samesite="strict")
        return response

    async def me(request):
        return _response(principal(request))

    async def profile(request):
        return _response(service.get_profile(principal(request)))

    async def demo_profile(request):
        identity = principal(request)
        from .resources import data_path
        data = json.loads(data_path("demo_users.json").read_text(encoding="utf-8"))
        user = next((u for u in data["users"] if u["user_id"] == identity.user_id), None)
        return _response({"synthetic": True, "confirmed": False, "data": user["demo_profile"] if user else {}})

    async def proposals(request):
        identity = principal(request)
        if request.method == "GET":
            return _response(service.list_proposals(identity))
        body = await _json_body(request)
        _fields(body, {"patch", "expected_version"}, {"patch", "expected_version"})
        if not isinstance(body["patch"], dict):
            raise _HTTPProblem("invalid_patch", "画像变更必须为对象。")
        return _response(service.propose_profile_update(identity, body["patch"], source="manual", expected_version=_version(body)), 201)

    async def confirm(request):
        identity = principal(request)
        body = await _json_body(request)
        _fields(body, {"expected_version", "expected_hash", "confirmed"}, {"expected_version", "expected_hash", "confirmed"})
        _confirmed(body)
        digest = _string(body, "expected_hash", 64)
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise _HTTPProblem("invalid_hash", "请提供页面显示提议的内容摘要。")
        return _response(service.confirm_profile_update(identity, request.path_params["proposal_id"], _version(body), digest, confirmed=True))

    async def proposal_get(request):
        return _response(service.get_proposal(principal(request), request.path_params["proposal_id"]))

    async def memory(request):
        identity = principal(request)
        body = await _json_body(request)
        _fields(body, {"confirmed"}, {"confirmed"})
        _confirmed(body)
        return _response(service.delete_memory(identity, confirmed=True))

    async def feedback(request):
        identity = principal(request)
        if request.method == "GET":
            return _response(service.list_feedback(identity))
        body = await _json_body(request)
        _fields(body, {"product_id", "action", "confirmed"}, {"product_id", "action", "confirmed"})
        _confirmed(body)
        return _response(service.record_feedback(identity, _string(body, "product_id", 100), action=_string(body, "action", 20), confirmed=True))

    async def task(request):
        identity = principal(request)
        body = await _json_body(request)
        _fields(body, {"message", "request_id"}, {"message"})
        request_id = _string(body, "request_id", 128) if body.get("request_id") is not None else None
        return _response(await AgentRunner(service, model_mode).run(identity, _string(body, "message"), request_id=request_id), 201)

    async def runs(request):
        return _response(service.list_runs(principal(request)))

    async def run(request):
        return _response(service.get_run(principal(request), request.path_params["run_id"]))

    async def terms(request):
        return _response(service.search_terms(principal(request), query=request.query_params.get("query", ""), as_of=request.query_params.get("as_of", "2026-10-03"), product_id=request.query_params.get("product_id")))

    async def term_answer(request):
        identity = principal(request)
        body = await _json_body(request)
        _fields(body, {"question", "as_of", "product_id"}, {"question"})
        as_of = _string(body, "as_of", 10) if "as_of" in body else "2026-10-03"
        product_id = _string(body, "product_id", 100) if body.get("product_id") is not None else None
        return _response(await TermQA(service, model_mode).answer(identity, _string(body, "question", 500), as_of, product_id))

    async def problem(request, exc):
        return _response({"error": exc.code, "message": exc.message}, exc.status_code)

    routes = [Route("/", home), Route("/health", health), Route("/api/demo-users", users),
              Route("/api/login", login, methods=["POST"]), Route("/api/logout", logout, methods=["POST"]),
              Route("/api/me", me), Route("/api/profile", profile), Route("/api/demo-profile", demo_profile),
              Route("/api/profile-proposals", proposals, methods=["GET", "POST"]),
              Route("/api/profile-proposals/{proposal_id}", proposal_get),
              Route("/api/profile-proposals/{proposal_id}/confirm", confirm, methods=["POST"]),
              Route("/api/memory", memory, methods=["DELETE"]), Route("/api/feedback", feedback, methods=["GET", "POST"]),
              Route("/api/tasks", task, methods=["POST"]), Route("/api/runs", runs), Route("/api/runs/{run_id}", run),
              Route("/api/terms", terms), Route("/api/term-answers", term_answer, methods=["POST"])]
    app = Starlette(routes=routes, exception_handlers={_HTTPProblem: problem, DomainError: problem})
    app.state.service, app.state.model_mode = service, model_mode
    app.add_middleware(SameOriginMiddleware)
    return app
