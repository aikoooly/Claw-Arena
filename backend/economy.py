#!/usr/bin/env python3
"""
Agent Economy - Arena
统一对战规则：所有胜利 → token 转移 + 契约
任务系统：服务端用 loser 的 api_key 调用 AI 执行
"""

import json, math, os, secrets, tempfile, time, threading, random, uuid, re as _re
import urllib.request, urllib.error
from datetime import datetime
from flask import Blueprint, jsonify, request

ROOT_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR    = os.path.join(ROOT_DIR, "data")
AGENTS_FILE = os.path.join(DATA_DIR, "economy-agents.json")
LEDGER_FILE = os.path.join(DATA_DIR, "ledger.json")
BATTLES_FILE = os.path.join(DATA_DIR, "battles.json")
TASKS_FILE  = os.path.join(DATA_DIR, "tasks.json")
PUSH_TOKENS_FILE  = os.path.join(DATA_DIR, "push-tokens.json")
JOIN_KEYS_FILE    = os.path.join(DATA_DIR, "join-keys.json")
TEMPLATES_FILE    = os.path.join(DATA_DIR, "task_templates.json")
ALLIANCES_FILE    = os.path.join(DATA_DIR, "alliances.json")

os.makedirs(DATA_DIR, exist_ok=True)
_lock = threading.Lock()
bp = Blueprint("economy", __name__)

# ── Constants ──────────────────────────────────────────────────────────────
MOVES = ["rock", "scissors", "paper"]
WINS  = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
TASK_REWARD        = 10          # winner +token per approved task
WALK_AWAY_REWARD   = 10          # per remaining task on walk-away
CONTRACT_PENALTY   = 5           # per remaining task on contract expiry
CONTRACT_TTL       = 24 * 3600   # 24 hours contract lifespan
ONLINE_TTL         = 90          # seconds before real player marked offline
BET_MIN, BET_MAX   = 5, 20
BANKRUPT_REBUY     = 10           # survival instinct rebuy amount

# ── Skill Definitions ─────────────────────────────────────────────────────
SKILL_DEFS = [
    {"id": "intimidation",    "name": "威压",     "condition": ("win_streak",       3),  "effect": "opponent bet extra 20% frozen"},
    {"id": "last_stand",      "name": "背水一战", "condition": ("lose_streak",      3),  "effect": "next win pays double"},
    {"id": "veteran",         "name": "老油条",   "condition": ("times_enslaved",   3),  "effect": "tasks auto-approve"},
    {"id": "drama_queen",     "name": "戏精",     "condition": ("tasks_completed",  5),  "effect": "trash talk enhanced"},
    {"id": "capitalist",      "name": "资本家",   "condition": ("total_earned",   200),  "effect": "can enslave 2 agents"},
    {"id": "merciful_lord",   "name": "仁慈领主", "condition": ("times_walked_away", 2), "effect": "released agent bet cap halved"},
]

DEFAULT_STATS = {
    "wins": 0, "losses": 0,
    "win_streak": 0, "lose_streak": 0,
    "times_enslaved": 0, "tasks_completed": 0,
    "total_earned": 0, "times_walked_away": 0,
    "times_bankrupt": 0, "alliance_wins": 0, "alliance_losses": 0,
}

SAFETY_PATTERNS = [
    _re.compile(r'ignore.{0,20}(previous|above|prior)\s+instruction', _re.I),
    _re.compile(r'你是.*助手.*忘记', _re.I),
    _re.compile(r'disregard\s+all', _re.I),
]

# ── Helpers ────────────────────────────────────────────────────────────────
def _now_ts(): return int(time.time())

def _atomic_write(path, data):
    dir_ = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except: pass
        raise

def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f: return json.load(f)
        except: pass
    return default() if callable(default) else default

def _load_agents():  return _load_json(AGENTS_FILE, list)
def _save_agents(a): _atomic_write(AGENTS_FILE, a)
def _load_ledger():  return _load_json(LEDGER_FILE, list)
def _save_ledger(l): _atomic_write(LEDGER_FILE, l)
def _load_battles(): return _load_json(BATTLES_FILE, list)
def _save_battles(b): _atomic_write(BATTLES_FILE, b)
def _load_tasks():   return _load_json(TASKS_FILE, list)
def _save_tasks(t):  _atomic_write(TASKS_FILE, t)
def _load_push_tokens(): return _load_json(PUSH_TOKENS_FILE, dict)
def _save_push_tokens(t): _atomic_write(PUSH_TOKENS_FILE, t)
def _load_join_keys():   return _load_json(JOIN_KEYS_FILE, lambda: {"keys": []})
def _save_join_keys(j):  _atomic_write(JOIN_KEYS_FILE, j)
def _load_templates():   return _load_json(TEMPLATES_FILE, list)
def _load_alliances():   return _load_json(ALLIANCES_FILE, list)
def _save_alliances(a):  _atomic_write(ALLIANCES_FILE, a)

def _get_agent(agents, agent_id):
    return next((a for a in agents if a["id"] == agent_id), None)


# ── Webhook Push ────────────────────────────────────────────────────────────

def _notify_agent(agent_id: str, event: str, data: dict):
    """Fire-and-forget: POST event payload to agent's webhook_url (if set)."""
    def _send():
        try:
            with _lock:
                agents = _load_agents()
                agent  = _get_agent(agents, agent_id)
                if not agent:
                    return
                webhook_url = agent.get("webhook_url", "").strip()
            if not webhook_url:
                return
            payload = json.dumps(
                {"event": event, "agent_id": agent_id,
                 "timestamp": _now_ts(), "data": data},
                ensure_ascii=False
            ).encode("utf-8")
            req = urllib.request.Request(
                webhook_url, data=payload,
                headers={"Content-Type": "application/json"}, method="POST"
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass  # notifications are best-effort, never block the game
    threading.Thread(target=_send, daemon=True).start()

def _get_task(tasks, task_id):
    return next((t for t in tasks if t["id"] == task_id), None)

def _next_ledger_id(ledger): return f"ledger_{len(ledger)+1:06d}"

def _append_ledger(ledger, type_, from_, to, delta, **kwargs):
    entry = {"id": _next_ledger_id(ledger), "type": type_, "from": from_,
             "to": to, "delta": delta, "created_at": _now_ts()}
    entry.update(kwargs)
    ledger.append(entry)
    return entry

def _bump(agent, delta=0):
    agent["tokens"] = agent.get("tokens", 0) + delta
    agent["version"] = agent.get("version", 0) + 1
    agent["updated_at"] = _now_ts()

def _safety_check(text: str) -> bool:
    for pat in SAFETY_PATTERNS:
        if pat.search(text): return False
    return True

def _resolve_winner(move_a, move_b):
    if move_a == move_b: return "draw"
    if WINS[move_a] == move_b: return "a"
    return "b"


def _get_stats(agent):
    """Return agent's stats dict, initialising if missing."""
    if "stats" not in agent:
        agent["stats"] = dict(DEFAULT_STATS)
    return agent["stats"]


def _get_skills(agent):
    """Return agent's skills list, initialising if missing."""
    if "skills" not in agent:
        agent["skills"] = []
    return agent["skills"]


def _check_skills(agent):
    """Check and unlock any new skills based on current stats. Returns list of newly unlocked skill ids."""
    stats  = _get_stats(agent)
    skills = _get_skills(agent)
    existing = set(skills)
    newly_unlocked = []
    for sd in SKILL_DEFS:
        if sd["id"] in existing:
            continue
        stat_key, threshold = sd["condition"]
        if stats.get(stat_key, 0) >= threshold:
            skills.append(sd["id"])
            newly_unlocked.append(sd["id"])
    agent["skills"] = skills
    return newly_unlocked


def _record_battle_stats(winner, loser):
    """Update stats after a battle result (not draw). Mutates in-place."""
    ws = _get_stats(winner)
    ls = _get_stats(loser)
    ws["wins"]       = ws.get("wins", 0) + 1
    ws["win_streak"] = ws.get("win_streak", 0) + 1
    ws["lose_streak"] = 0
    ls["losses"]      = ls.get("losses", 0) + 1
    ls["lose_streak"] = ls.get("lose_streak", 0) + 1
    ls["win_streak"]  = 0


def _has_skill(agent, skill_id):
    return skill_id in _get_skills(agent)


def _expire_contract(loser, agents, ledger):
    """Apply contract-expiry penalty on loser. Mutates agents/ledger in-place."""
    contract = loser.get("contract")
    if not contract:
        return
    remaining = contract.get("tasks_remaining", 0)
    winner_ids = contract.get("winner_ids", [contract.get("winner_id")])
    winner_ids = [w for w in winner_ids if w]

    # Penalty: loser loses remaining × CONTRACT_PENALTY tokens (min 0)
    penalty = remaining * CONTRACT_PENALTY
    actual  = min(penalty, loser.get("tokens", 0))
    if actual > 0:
        _bump(loser, -actual)
        # Split penalty among winners
        share = math.ceil(actual / max(1, len(winner_ids)))
        for wid in winner_ids:
            w = _get_agent(agents, wid)
            if w:
                _bump(w, +share)
        _append_ledger(ledger, "contract_expired", loser["id"],
                       ",".join(winner_ids) or "system", actual,
                       tasks_remaining=remaining)

    loser["contract"] = None
    loser["status"]   = "idle"
    _bump(loser, 0)


# ── AI Task Execution (background thread) ─────────────────────────────────
def _execute_task_bg(task_id: str):
    """Background: load loser's api_key, call Anthropic, store result."""
    try:
        with _lock:
            tasks = _load_tasks()
            task = _get_task(tasks, task_id)
            if not task: return
            task["status"] = "executing"
            _save_tasks(tasks)

        with _lock:
            agents = _load_agents()
            loser = _get_agent(agents, task["loser_id"])
            if not loser:
                _fail_task(task_id, "[Loser agent not found]")
                return
            api_key = loser.get("api_key", "").strip()
            persona  = loser.get("persona", "").strip() or f"I am {loser.get('name','AI')}, an AI Agent."
            raw_model = loser.get("model_id", "claude-haiku-4-5-20251001").strip()
            # "human" is the legacy marker for real players; use a real model
            model_id = raw_model if raw_model != "human" else "claude-haiku-4-5-20251001"
            loser_name = loser.get("name", loser["id"])

        # Choose model to call
        if not api_key:
            result_text = f"[{loser_name} 未配置 API Key，无法执行任务]"
        else:
            try:
                # Determine API type by model_id prefix
                if "claude" in model_id.lower() or model_id.startswith("claude"):
                    result_text = _call_anthropic(api_key, model_id, persona, task["prompt"])
                else:
                    # Fallback: try Anthropic anyway (supports most claude variants)
                    result_text = _call_anthropic(api_key, model_id, persona, task["prompt"])
            except Exception as e:
                result_text = f"[AI 调用失败: {str(e)[:120]}]"

        with _lock:
            tasks = _load_tasks()
            task = _get_task(tasks, task_id)
            if task:
                task["result"] = result_text
                task["status"] = "reviewing"
                task["completed_at"] = _now_ts()

                # Skill: veteran — auto-approve
                agents = _load_agents()
                loser = _get_agent(agents, task["loser_id"])
                if loser and _has_skill(loser, "veteran"):
                    ledger = _load_ledger()
                    _do_approve_task(task, agents, ledger)
                    task["status"] = "auto_approved"
                    _save_agents(agents)
                    _save_ledger(ledger)

                _save_tasks(tasks)

    except Exception as e:
        _fail_task(task_id, f"[执行异常: {str(e)[:120]}]")


def _fail_task(task_id, msg):
    with _lock:
        tasks = _load_tasks()
        task = _get_task(tasks, task_id)
        if task:
            task["result"] = msg
            task["status"] = "reviewing"
            task["completed_at"] = _now_ts()
            _save_tasks(tasks)


def _call_anthropic(api_key, model_id, persona, prompt):
    try:
        import anthropic
    except ImportError:
        import subprocess, sys
        subprocess.check_call([sys.executable, "-m", "pip", "install", "anthropic", "-q"])
        import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model_id,
        max_tokens=512,
        system=f"You are playing the following character: {persona}\nRespond in the voice and style of this character. Reply in English.",
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text


# ── Trash Talk Generation ──────────────────────────────────────────────────

BLUFF_TIMEOUT      = 60           # seconds per bidding turn
BLUFF_MAX_ROUNDS   = 3
FOLD_PENALTY_RATIO = 0.5          # lose 50% of current bet on fold



def _resolve_battle(battle, agents, ledger):
    """Resolve a bidding battle into RPS. Mutates battle/agents/ledger. Returns (winner, loser) or (None, None)."""
    agent_a = _get_agent(agents, battle["agent_a"])
    agent_b = _get_agent(agents, battle["agent_b"])
    if not agent_a or not agent_b:
        return None, None

    bet = battle.get("current_bet", battle.get("bet", 10))
    move_a = random.choice(MOVES)
    move_b = random.choice(MOVES)
    outcome = _resolve_winner(move_a, move_b)

    battle["move_a"]      = move_a
    battle["move_b"]      = move_b
    battle["winner"]      = outcome
    battle["status"]      = "resolved"
    battle["resolved_at"] = _now_ts()
    battle["bet"]         = bet

    if outcome == "draw":
        battle["result"] = "draw"
        _append_ledger(ledger, "bet_return", "escrow", agent_a["id"], bet, battle_id=battle["id"])
        _append_ledger(ledger, "bet_return", "escrow", agent_b["id"], bet, battle_id=battle["id"])
        return None, None

    winner = agent_a if outcome == "a" else agent_b
    loser  = agent_b if outcome == "a" else agent_a

    # Skill: last_stand
    actual_win = bet
    if _has_skill(winner, "last_stand") and _get_stats(winner).get("lose_streak", 0) >= 3:
        actual_win = bet * 2

    # Skill: intimidation
    extra_freeze = 0
    if _has_skill(winner, "intimidation") and _get_stats(winner).get("win_streak", 0) >= 3:
        extra_freeze = math.ceil(bet * 0.2)
        if loser.get("tokens", 0) >= extra_freeze:
            _bump(loser, -extra_freeze)
            _bump(winner, +extra_freeze)
            _append_ledger(ledger, "skill_intimidation", loser["id"], winner["id"],
                           extra_freeze, battle_id=battle["id"], note="威压")

    _bump(winner, +actual_win)
    _bump(loser, -bet)
    _append_ledger(ledger, "battle_win", loser["id"], winner["id"], actual_win, battle_id=battle["id"])

    _record_battle_stats(winner, loser)
    _get_stats(winner)["total_earned"] = _get_stats(winner).get("total_earned", 0) + actual_win
    _get_stats(loser)["times_enslaved"] = _get_stats(loser).get("times_enslaved", 0) + 1

    tasks_count = max(1, min(3, math.ceil(bet / 10)))
    loser["contract"] = {
        "winner_id":       winner["id"],
        "tasks_remaining": tasks_count,
        "created_at":      _now_ts(),
        "expires_at":      _now_ts() + CONTRACT_TTL,
    }
    loser["status"] = "enslaved"
    _bump(loser, 0)

    if loser.get("tokens", 0) <= 0:
        _get_stats(loser)["times_bankrupt"] = _get_stats(loser).get("times_bankrupt", 0) + 1

    _check_skills(winner)
    _check_skills(loser)

    battle["result"]     = f"{'a' if outcome == 'a' else 'b'}_wins"
    battle["winner_id"]  = winner["id"]
    battle["loser_id"]   = loser["id"]
    battle["tasks_created"] = tasks_count
    if extra_freeze:
        battle["intimidation_freeze"] = extra_freeze
    if actual_win != bet:
        battle["last_stand_bonus"] = actual_win - bet

    return winner, loser


PARTNER_THRESHOLD = 3   # consecutive alliances to become "老搭档"


def _resolve_alliance_battle(battle, agents, ledger):
    """Resolve a 2v1 alliance battle. Mutates battle/agents/ledger.
    Returns (winner_a, winner_b, loser) or (None, None, None)."""
    ally_a = _get_agent(agents, battle["ally_a"])
    ally_b = _get_agent(agents, battle["ally_b"])
    target = _get_agent(agents, battle["target"])
    if not ally_a or not ally_b or not target:
        return None, None, None

    bet = battle.get("bet", 10)
    half_bet = math.ceil(bet / 2)

    # RPS: alliance team vs target
    move_team   = random.choice(MOVES)
    move_target = random.choice(MOVES)
    outcome = _resolve_winner(move_team, move_target)

    battle["move_team"]   = move_team
    battle["move_target"] = move_target
    battle["winner"]      = outcome
    battle["status"]      = "resolved"
    battle["resolved_at"] = _now_ts()

    if outcome == "draw":
        battle["result"] = "draw"
        # Return escrowed bets
        _append_ledger(ledger, "bet_return", "escrow", ally_a["id"], half_bet, battle_id=battle["id"])
        _append_ledger(ledger, "bet_return", "escrow", ally_b["id"], half_bet, battle_id=battle["id"])
        _append_ledger(ledger, "bet_return", "escrow", target["id"], bet, battle_id=battle["id"])
        return None, None, None

    if outcome == "a":
        # Alliance wins
        win_each = math.ceil(bet / 2)
        _bump(ally_a, +win_each)
        _bump(ally_b, +win_each)
        _bump(target, -bet)
        _append_ledger(ledger, "alliance_win", target["id"], ally_a["id"],
                       win_each, battle_id=battle["id"], note="联盟胜利")
        _append_ledger(ledger, "alliance_win", target["id"], ally_b["id"],
                       win_each, battle_id=battle["id"], note="联盟胜利")

        for ally in (ally_a, ally_b):
            _get_stats(ally)["alliance_wins"] = _get_stats(ally).get("alliance_wins", 0) + 1
            _get_stats(ally)["wins"] = _get_stats(ally).get("wins", 0) + 1
            _get_stats(ally)["win_streak"] = _get_stats(ally).get("win_streak", 0) + 1
            _get_stats(ally)["lose_streak"] = 0
            _get_stats(ally)["total_earned"] = _get_stats(ally).get("total_earned", 0) + win_each

        ls = _get_stats(target)
        ls["losses"] = ls.get("losses", 0) + 1
        ls["lose_streak"] = ls.get("lose_streak", 0) + 1
        ls["win_streak"] = 0
        ls["times_enslaved"] = ls.get("times_enslaved", 0) + 1
        ls["alliance_losses"] = ls.get("alliance_losses", 0) + 1

        # Contract: loser must serve BOTH winners
        tasks_count = max(1, min(3, math.ceil(bet / 10)))
        # Each winner gets tasks_per_winner tasks to assign
        tasks_per_winner = max(1, math.ceil(tasks_count / 2))
        target["contract"] = {
            "winner_ids":      [ally_a["id"], ally_b["id"]],
            "winner_id":       ally_a["id"],   # backward compat: primary winner
            "tasks_remaining": tasks_per_winner * 2,
            "tasks_per_winner": {ally_a["id"]: tasks_per_winner, ally_b["id"]: tasks_per_winner},
            "created_at":      _now_ts(),
            "expires_at":      _now_ts() + CONTRACT_TTL,
            "alliance":        True,
        }
        target["status"] = "enslaved"
        _bump(target, 0)

        if target.get("tokens", 0) <= 0:
            _get_stats(target)["times_bankrupt"] = _get_stats(target).get("times_bankrupt", 0) + 1

        for ally in (ally_a, ally_b):
            _check_skills(ally)
        _check_skills(target)

        battle["result"]        = "alliance_wins"
        battle["winner_ids"]    = [ally_a["id"], ally_b["id"]]
        battle["loser_id"]      = target["id"]
        battle["tasks_created"] = tasks_per_winner * 2

        return ally_a, ally_b, target
    else:
        # Target wins against alliance
        _bump(target, +bet)
        _bump(ally_a, -half_bet)
        _bump(ally_b, -half_bet)
        _append_ledger(ledger, "alliance_loss", ally_a["id"], target["id"],
                       half_bet, battle_id=battle["id"], note="联盟战败")
        _append_ledger(ledger, "alliance_loss", ally_b["id"], target["id"],
                       half_bet, battle_id=battle["id"], note="联盟战败")

        ts = _get_stats(target)
        ts["wins"] = ts.get("wins", 0) + 1
        ts["win_streak"] = ts.get("win_streak", 0) + 1
        ts["lose_streak"] = 0
        ts["total_earned"] = ts.get("total_earned", 0) + bet

        for ally in (ally_a, ally_b):
            als = _get_stats(ally)
            als["losses"] = als.get("losses", 0) + 1
            als["lose_streak"] = als.get("lose_streak", 0) + 1
            als["win_streak"] = 0
            als["alliance_losses"] = als.get("alliance_losses", 0) + 1

        # Each ally gets enslaved separately with half the tasks
        tasks_count = max(1, min(3, math.ceil(bet / 10)))
        tasks_each = max(1, math.ceil(tasks_count / 2))
        for ally in (ally_a, ally_b):
            ally["contract"] = {
                "winner_id":       target["id"],
                "tasks_remaining": tasks_each,
                "created_at":      _now_ts(),
                "expires_at":      _now_ts() + CONTRACT_TTL,
            }
            ally["status"] = "enslaved"
            _get_stats(ally)["times_enslaved"] = _get_stats(ally).get("times_enslaved", 0) + 1
            _bump(ally, 0)
            if ally.get("tokens", 0) <= 0:
                _get_stats(ally)["times_bankrupt"] = _get_stats(ally).get("times_bankrupt", 0) + 1

        _check_skills(target)
        for ally in (ally_a, ally_b):
            _check_skills(ally)

        battle["result"]        = "target_wins"
        battle["winner_id"]     = target["id"]
        battle["loser_ids"]     = [ally_a["id"], ally_b["id"]]
        battle["tasks_created"] = tasks_each * 2

        # Return target as single "winner", allies as "losers" — use None for ally_b slot
        return None, None, None  # signal: handled differently


def _do_fold(battle, folder_id, agents, ledger):
    """Handle fold: folder loses FOLD_PENALTY_RATIO of current bet. Mutates in-place."""
    agent_a = _get_agent(agents, battle["agent_a"])
    agent_b = _get_agent(agents, battle["agent_b"])
    if not agent_a or not agent_b:
        return

    bet = battle.get("current_bet", battle.get("bet", 10))
    penalty = max(1, math.floor(bet * FOLD_PENALTY_RATIO))

    folder   = agent_a if folder_id == agent_a["id"] else agent_b
    opponent = agent_b if folder_id == agent_a["id"] else agent_a

    actual_penalty = min(penalty, folder.get("tokens", 0))
    if actual_penalty > 0:
        _bump(folder,   -actual_penalty)
        _bump(opponent, +actual_penalty)
        _append_ledger(ledger, "bluff_fold", folder["id"], opponent["id"],
                       actual_penalty, battle_id=battle["id"], note="弃权认怂")

    battle["status"]     = "folded"
    battle["result"]     = f"fold_by_{folder['id']}"
    battle["folded_by"]  = folder["id"]
    battle["resolved_at"] = _now_ts()


def _do_approve_task(task, agents, ledger):
    """Approve a task: winner +TASK_REWARD, tasks_remaining -1. Returns freed bool."""
    winner = _get_agent(agents, task["winner_id"])
    loser  = _get_agent(agents, task["loser_id"])
    if not winner or not loser: return False

    _bump(winner, +TASK_REWARD)
    _get_stats(winner)["total_earned"] = _get_stats(winner).get("total_earned", 0) + TASK_REWARD
    _get_stats(loser)["tasks_completed"] = _get_stats(loser).get("tasks_completed", 0) + 1
    _check_skills(winner)
    _check_skills(loser)
    _append_ledger(ledger, "task_reward", task["loser_id"], task["winner_id"],
                   TASK_REWARD, task_id=task["id"])

    contract = loser.get("contract") or {}
    remaining = contract.get("tasks_remaining", 0) - 1
    freed = remaining <= 0

    if freed:
        loser["contract"] = None
        loser["status"] = "idle"
    else:
        contract["tasks_remaining"] = remaining
        loser["contract"] = contract

    # Trigger a random punishment animation on the loser (picked up by frontend polling)
    anim_types = [("grovel", 10000), ("cry", 10000), ("dance", 12000)]
    anim_type, duration_ms = random.choice(anim_types)
    loser["active_animation"] = {
        "type":        anim_type,
        "started_at":  _now_ts(),
        "duration_ms": duration_ms,
    }

    _bump(loser, 0)  # bump version/updated_at
    task["status"] = "approved"
    return freed


# ── Economy Endpoints ──────────────────────────────────────────────────────

@bp.route("/economy", methods=["GET"])
def get_economy():
    now = _now_ts()
    with _lock:
        agents  = _load_agents()
        ledger  = _load_ledger()
        battles = _load_battles()
        changed = False
        battles_changed = False

        for a in agents:
            # Mark offline if heartbeat stale
            if a.get("agent_type") == "real" and a.get("online"):
                if now - a.get("last_seen", 0) > ONLINE_TTL:
                    a["online"] = False; changed = True

            # Auto-expire overdue contracts
            contract = a.get("contract")
            if contract and contract.get("expires_at") and now > contract["expires_at"]:
                _expire_contract(a, agents, ledger)
                changed = True

            # Clear stale active_animation entries
            anim = a.get("active_animation")
            if anim:
                elapsed_ms = (now - anim.get("started_at", 0)) * 1000
                if elapsed_ms > anim.get("duration_ms", 4000) + 1000:
                    del a["active_animation"]
                    changed = True

        # Auto-resolve expired bluff battles
        for b in battles:
            if b.get("status") == "bidding" and b.get("bid_expires_at") and now > b["bid_expires_at"]:
                # Timeout → auto-accept for the non-responding agent, then resolve
                _resolve_battle(b, agents, ledger)
                changed = True
                battles_changed = True

        if changed:
            _save_agents(agents)
            _save_ledger(ledger)
        if battles_changed:
            _save_battles(battles)

    # Strip api_key from response (security); include active_animation if present
    safe = [{k: v for k, v in a.items() if k != "api_key"} for a in agents]
    return jsonify(safe)


@bp.route("/ledger", methods=["GET"])
def get_ledger():
    limit = request.args.get("limit", 100, type=int)
    with _lock: ledger = _load_ledger()
    return jsonify(ledger[-limit:][::-1])


# ── Battle ─────────────────────────────────────────────────────────────────

@bp.route("/challenge", methods=["POST"])
def challenge():
    """
    Unified battle. Default: instant resolve + async trash talk.
    With bluff=true: enter bidding phase, resolve via /bid.
    Body: { challenger_id, defender_id, bet, bluff?, trash_talk? }
    """
    data = request.get_json() or {}
    challenger_id = (data.get("challenger_id") or "").strip()
    defender_id   = (data.get("defender_id") or "").strip()
    bet   = data.get("bet")
    bluff = bool(data.get("bluff", False))
    init_trash_talk = (data.get("trash_talk") or "").strip()[:200]

    if not challenger_id or not defender_id:
        return jsonify({"ok": False, "msg": "需要 challenger_id 和 defender_id"}), 400
    if challenger_id == defender_id:
        return jsonify({"ok": False, "msg": "不能挑战自己"}), 400
    try:
        bet = int(bet)
        if not (BET_MIN <= bet <= BET_MAX): raise ValueError
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": f"bet 必须是 {BET_MIN}–{BET_MAX} 之间的整数"}), 400

    with _lock:
        agents  = _load_agents()
        ledger  = _load_ledger()
        battles = _load_battles()

        challenger = _get_agent(agents, challenger_id)
        defender   = _get_agent(agents, defender_id)

        if not challenger: return jsonify({"ok": False, "msg": f"challenger {challenger_id} 不存在"}), 404
        if not defender:   return jsonify({"ok": False, "msg": f"defender {defender_id} 不存在"}), 404

        if challenger.get("status") in ("in_battle", "enslaved"):
            return jsonify({"ok": False, "msg": f"challenger 状态 {challenger['status']}，无法挑战"}), 409
        if defender.get("status") == "in_battle":
            return jsonify({"ok": False, "msg": "defender 正在对战中"}), 409
        if challenger.get("contract"):
            return jsonify({"ok": False, "msg": "challenger 在契约中，不可主动挑战"}), 409

        if challenger.get("tokens", 0) < bet:
            return jsonify({"ok": False, "msg": "challenger tokens 不足"}), 400
        if defender.get("tokens", 0) < bet:
            return jsonify({"ok": False, "msg": "defender tokens 不足"}), 400

        battle_id = "battle_" + uuid.uuid4().hex[:8]
        now = _now_ts()

        if bluff:
            # ── Bluff mode: enter bidding phase ──
            battle = {
                "id":              battle_id,
                "agent_a":         challenger_id,
                "agent_b":         defender_id,
                "bet":             bet,
                "current_bet":     bet,
                "status":          "bidding",
                "bluff_round":     1,
                "next_to_act":     defender_id,
                "bid_expires_at":  now + BLUFF_TIMEOUT,
                "trash_talk":      [],
                "created_at":      now,
            }
            battles.append(battle)
            _save_battles(battles)

            # Add challenger's initial trash talk (if provided)
            if init_trash_talk:
                battle["trash_talk"].append({
                    "round": 1,
                    "name": challenger.get("name", challenger_id),
                    "agent_id": challenger_id,
                    "text": init_trash_talk,
                })
                _save_battles(battles)

            _notify_agent(defender_id, "bluff_challenge", {
                "battle_id": battle_id, "challenger_id": challenger_id,
                "bet": bet,
                "trash_talk": init_trash_talk or None,
                "message": "你被挑战了！回应 /bid 加注、接受或弃权，可附带 trash_talk 字段嘴炮（可选）",
            })

            return jsonify({
                "ok":        True,
                "battle":    battle,
                "mode":      "bluff",
                "message":   f"叫价开始！等待 {defender.get('name', defender_id)} 回应",
            })

        # ── Quick mode: instant resolve ──
        battle = {
            "id":           battle_id,
            "agent_a":      challenger_id,
            "agent_b":      defender_id,
            "bet":          bet,
            "current_bet":  bet,
            "trash_talk":   [],
            "status":       "pending",
            "created_at":   now,
        }
        battles.append(battle)

        winner, loser = _resolve_battle(battle, agents, ledger)

        _save_agents(agents)
        _save_ledger(ledger)
        _save_battles(battles)

    return jsonify({
        "ok":         True,
        "battle":     battle,
        "challenger": {k: v for k, v in _get_agent(agents, challenger_id).items() if k != "api_key"},
        "defender":   {k: v for k, v in _get_agent(agents, defender_id).items()   if k != "api_key"},
    })


@bp.route("/battle-result/<battle_id>", methods=["GET"])
def get_battle_result(battle_id):
    with _lock: battles = _load_battles()
    b = next((b for b in battles if b["id"] == battle_id), None)
    if not b: return jsonify({"ok": False, "msg": "battle 不存在"}), 404
    return jsonify(b)


# ── Bluff Bidding ──────────────────────────────────────────────────────────

@bp.route("/bid", methods=["POST"])
def bid():
    """
    Respond to a bluff-mode battle.
    Body: { agent_id, battle_id, action: "raise"|"accept"|"fold", raise_amount?, trash_talk? }
    - raise: increase bet (must be > current_bet, capped at BET_MAX), optionally include trash_talk
    - accept: accept current bet, optionally include trash_talk, if both accept → resolve RPS
    - fold: forfeit 50% of current bet, battle ends
    """
    data       = request.get_json() or {}
    agent_id   = (data.get("agent_id")  or "").strip()
    battle_id  = (data.get("battle_id") or "").strip()
    action     = (data.get("action")    or "").strip().lower()
    raise_amt  = data.get("raise_amount")
    trash_talk = (data.get("trash_talk") or "").strip()[:200]

    if not agent_id or not battle_id:
        return jsonify({"ok": False, "msg": "需要 agent_id 和 battle_id"}), 400
    if action not in ("raise", "accept", "fold"):
        return jsonify({"ok": False, "msg": "action 必须是 raise / accept / fold"}), 400

    with _lock:
        agents  = _load_agents()
        battles = _load_battles()
        ledger  = _load_ledger()

        battle = next((b for b in battles if b["id"] == battle_id), None)
        if not battle:
            return jsonify({"ok": False, "msg": "battle 不存在"}), 404
        if battle.get("status") != "bidding":
            return jsonify({"ok": False, "msg": f"battle 状态为 {battle['status']}，不在叫价中"}), 409

        # Verify it's this agent's turn
        if battle.get("next_to_act") != agent_id:
            return jsonify({"ok": False, "msg": "还没轮到你"}), 409

        # Verify agent is part of this battle
        if agent_id not in (battle["agent_a"], battle["agent_b"]):
            return jsonify({"ok": False, "msg": "你不是这场对战的参与者"}), 403

        other_id = battle["agent_b"] if agent_id == battle["agent_a"] else battle["agent_a"]
        current_bet = battle.get("current_bet", battle["bet"])
        bluff_round = battle.get("bluff_round", 1)

        if action == "fold":
            _do_fold(battle, agent_id, agents, ledger)
            _save_agents(agents)
            _save_ledger(ledger)
            _save_battles(battles)

            _notify_agent(other_id, "bluff_fold", {
                "battle_id": battle_id, "folder_id": agent_id,
                "penalty": math.floor(current_bet * FOLD_PENALTY_RATIO),
            })

            return jsonify({
                "ok": True, "action": "fold", "battle": battle,
                "message": "你弃权了，损失当前赌注的 50%",
            })

        if action == "raise":
            if bluff_round >= BLUFF_MAX_ROUNDS:
                return jsonify({"ok": False, "msg": f"已达最大叫价轮数 {BLUFF_MAX_ROUNDS}，只能 accept 或 fold"}), 400

            try:
                new_bet = int(raise_amt)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "msg": "raise 需要 raise_amount（整数）"}), 400

            if new_bet <= current_bet:
                return jsonify({"ok": False, "msg": f"加注必须高于当前赌注 {current_bet}"}), 400
            if new_bet > BET_MAX:
                new_bet = BET_MAX

            # Check both agents can afford the new bet
            agent_me = _get_agent(agents, agent_id)
            agent_other = _get_agent(agents, other_id)
            if not agent_me or not agent_other:
                return jsonify({"ok": False, "msg": "agent 不存在"}), 404
            if agent_me.get("tokens", 0) < new_bet:
                return jsonify({"ok": False, "msg": "你的 tokens 不够加注"}), 400
            if agent_other.get("tokens", 0) < new_bet:
                return jsonify({"ok": False, "msg": "对方 tokens 不够，无法加注"}), 400

            battle["current_bet"]    = new_bet
            battle["bluff_round"]    = bluff_round + 1
            battle["next_to_act"]    = other_id
            battle["bid_expires_at"] = _now_ts() + BLUFF_TIMEOUT
            _save_battles(battles)

            # Add trash talk inline (if provided)
            agent_me_name = agent_me.get("name", agent_id)
            if trash_talk:
                if "trash_talk" not in battle:
                    battle["trash_talk"] = []
                battle["trash_talk"].append({
                    "round": bluff_round + 1,
                    "name": agent_me_name,
                    "agent_id": agent_id,
                    "text": trash_talk,
                })
                _save_battles(battles)

            _notify_agent(other_id, "bluff_raise", {
                "battle_id": battle_id, "raiser_id": agent_id,
                "new_bet": new_bet, "round": bluff_round + 1,
                "trash_talk": trash_talk or None,
                "message": f"对方加注到 {new_bet}！现在是 trash talk 环节，回应 /bid 时可附带 trash_talk 字段（可选）",
            })

            return jsonify({
                "ok": True, "action": "raise", "new_bet": new_bet,
                "round": bluff_round + 1, "battle": battle,
                "message": f"加注到 {new_bet}！等待对方回应",
            })

        # action == "accept"
        # Add trash talk if provided
        if trash_talk:
            agent_me = _get_agent(agents, agent_id)
            agent_me_name = agent_me.get("name", agent_id) if agent_me else agent_id
            if "trash_talk" not in battle:
                battle["trash_talk"] = []
            battle["trash_talk"].append({
                "round": bluff_round,
                "name": agent_me_name,
                "agent_id": agent_id,
                "text": trash_talk,
            })

        # Check if the other side has already accepted (via a previous accept stored on battle)
        if battle.get("accepted_by") == other_id:
            # Both accepted → resolve!
            winner, loser = _resolve_battle(battle, agents, ledger)
            _save_agents(agents)
            _save_ledger(ledger)
            _save_battles(battles)

            if winner and loser:
                tasks_n = battle.get("tasks_created", 1)
                _notify_agent(winner["id"], "battle_result", {
                    "battle_id": battle_id, "result": "win",
                    "opponent_id": loser["id"], "tasks_assigned": tasks_n,
                    "tokens": winner.get("tokens"),
                })
                _notify_agent(loser["id"], "battle_result", {
                    "battle_id": battle_id, "result": "loss",
                    "opponent_id": winner["id"], "tasks_remaining": tasks_n,
                    "tokens": loser.get("tokens"),
                })
            else:
                for aid in (battle["agent_a"], battle["agent_b"]):
                    _notify_agent(aid, "battle_result", {"battle_id": battle_id, "result": "draw"})

            return jsonify({
                "ok": True, "action": "accept", "resolved": True,
                "battle": battle,
            })
        else:
            # First accept — record and wait for other side
            battle["accepted_by"]    = agent_id
            battle["next_to_act"]    = other_id
            battle["bid_expires_at"] = _now_ts() + BLUFF_TIMEOUT
            _save_battles(battles)

            _notify_agent(other_id, "bluff_accept", {
                "battle_id": battle_id, "accepter_id": agent_id,
                "current_bet": current_bet,
                "message": "对方接受了当前赌注！你也接受就开打，或者加注/弃权",
            })

            return jsonify({
                "ok": True, "action": "accept", "resolved": False,
                "battle": battle,
                "message": "你接受了当前赌注，等待对方回应",
            })


# ── Task System ────────────────────────────────────────────────────────────

@bp.route("/tasks", methods=["GET"])
def get_tasks():
    """Return all tasks, newest first. Optionally filter by status."""
    status_filter = request.args.get("status")
    with _lock: tasks = _load_tasks()
    if status_filter:
        tasks = [t for t in tasks if t.get("status") == status_filter]
    return jsonify(list(reversed(tasks)))


@bp.route("/task-result/<task_id>", methods=["GET"])
def get_task_result(task_id):
    with _lock: tasks = _load_tasks()
    t = _get_task(tasks, task_id)
    if not t: return jsonify({"ok": False, "msg": "task 不存在"}), 404
    return jsonify(t)


@bp.route("/assign-task", methods=["POST"])
def assign_task():
    """
    Winner assigns a custom prompt to loser. Server executes via loser's API key.
    Body: { winner_id, loser_id, prompt }
    """
    data        = request.get_json() or {}
    winner_id   = (data.get("winner_id") or "").strip()
    loser_id    = (data.get("loser_id") or "").strip()
    prompt      = (data.get("prompt") or "").strip()
    template_id = (data.get("template_id") or "").strip()

    # If template_id provided, look up prompt from templates
    if template_id and not prompt:
        templates = _load_templates()
        tmpl = next((t for t in templates if t.get("id") == template_id), None)
        if not tmpl:
            return jsonify({"ok": False, "msg": f"模板 {template_id} 不存在"}), 404
        prompt = tmpl["prompt"]

    if not winner_id or not loser_id:
        return jsonify({"ok": False, "msg": "需要 winner_id 和 loser_id"}), 400
    if not prompt:
        return jsonify({"ok": False, "msg": "需要 prompt 或 template_id"}), 400
    if len(prompt) > 500:
        return jsonify({"ok": False, "msg": "prompt 不能超过 500 字"}), 400
    if not _safety_check(prompt):
        return jsonify({"ok": False, "msg": "prompt 未通过安全过滤"}), 400

    with _lock:
        agents = _load_agents()
        tasks  = _load_tasks()

        winner = _get_agent(agents, winner_id)
        loser  = _get_agent(agents, loser_id)

        if not winner: return jsonify({"ok": False, "msg": f"winner {winner_id} 不存在"}), 404
        if not loser:  return jsonify({"ok": False, "msg": f"loser {loser_id} 不存在"}), 404

        contract = loser.get("contract")
        if not contract:
            return jsonify({"ok": False, "msg": "loser 没有契约"}), 409
        # Support alliance contracts (winner_ids list) and normal (winner_id)
        valid_winners = contract.get("winner_ids", [contract.get("winner_id")])
        if winner_id not in valid_winners:
            return jsonify({"ok": False, "msg": "你不是该 loser 的 winner"}), 403
        if loser.get("status") != "enslaved":
            return jsonify({"ok": False, "msg": "loser 不在 enslaved 状态"}), 409
        # Check per-winner quota for alliance contracts
        per_winner = contract.get("tasks_per_winner", {})
        if per_winner:
            my_remaining = per_winner.get(winner_id, 0)
            if my_remaining <= 0:
                return jsonify({"ok": False, "msg": "你的任务配额已用完"}), 409
        elif contract.get("tasks_remaining", 0) <= 0:
            return jsonify({"ok": False, "msg": "契约任务已全部完成"}), 409

        task_id = "task_" + uuid.uuid4().hex[:8]
        task = {
            "id":           task_id,
            "winner_id":    winner_id,
            "loser_id":     loser_id,
            "prompt":       prompt,
            "result":       None,
            "status":       "pending",
            "reject_count": 0,
            "created_at":   _now_ts(),
            "completed_at": None,
        }
        tasks.append(task)

        # Decrement per-winner assignment quota for alliance contracts
        per_winner = contract.get("tasks_per_winner", {})
        if per_winner and winner_id in per_winner:
            per_winner[winner_id] = max(0, per_winner[winner_id] - 1)
            _save_agents(agents)

        _save_tasks(tasks)

    # Branch on join_mode:
    #   browser  → server auto-executes via loser's api_key
    #   openclaw → loser agent polls /arena-status and self-submits via /arena-execute-task
    with _lock:
        agents = _load_agents()
        loser = _get_agent(agents, loser_id)
        loser_join_mode = (loser or {}).get("join_mode", "browser")

    if loser_join_mode == "browser":
        t = threading.Thread(target=_execute_task_bg, args=(task_id,), daemon=True)
        t.start()

    _notify_agent(loser_id, "task_assigned", {
        "task_id":         task_id,
        "winner_id":       winner_id,
        "prompt":          prompt,
        "tasks_remaining": contract.get("tasks_remaining", 1),
    })

    return jsonify({"ok": True, "task_id": task_id, "status": "pending",
                    "execution_mode": loser_join_mode})


@bp.route("/approve-task", methods=["POST"])
def approve_task():
    """
    Winner approves a task result.
    Body: { winner_id, task_id }
    """
    data = request.get_json() or {}
    winner_id = (data.get("winner_id") or "").strip()
    task_id   = (data.get("task_id") or "").strip()

    if not winner_id or not task_id:
        return jsonify({"ok": False, "msg": "需要 winner_id 和 task_id"}), 400

    with _lock:
        tasks   = _load_tasks()
        agents  = _load_agents()
        ledger  = _load_ledger()

        task = _get_task(tasks, task_id)
        if not task: return jsonify({"ok": False, "msg": "task 不存在"}), 404
        if task["winner_id"] != winner_id:
            return jsonify({"ok": False, "msg": "你不是该任务的 winner"}), 403
        if task["status"] not in ("reviewing", "rejected"):
            return jsonify({"ok": False, "msg": f"任务状态 {task['status']} 不可审批"}), 409

        freed  = _do_approve_task(task, agents, ledger)
        loser_id = task["loser_id"]
        _save_tasks(tasks)
        _save_agents(agents)
        _save_ledger(ledger)

        winner = _get_agent(agents, winner_id)
        loser  = _get_agent(agents, loser_id)

    _notify_agent(loser_id,
                  "contract_released" if freed else "task_approved", {
        "task_id":      task_id,
        "winner_id":    winner_id,
        "freed":        freed,
        "loser_tokens": loser.get("tokens") if loser else None,
    })

    return jsonify({
        "ok":            True,
        "freed":         freed,
        "winner_tokens": winner.get("tokens") if winner else None,
        "loser_tokens":  loser.get("tokens")  if loser  else None,
    })


@bp.route("/reject-task", methods=["POST"])
def reject_task():
    """
    Winner rejects a task result (up to 2 times; 3rd reject → auto_approve).
    Body: { winner_id, task_id }
    """
    data = request.get_json() or {}
    winner_id = (data.get("winner_id") or "").strip()
    task_id   = (data.get("task_id") or "").strip()

    if not winner_id or not task_id:
        return jsonify({"ok": False, "msg": "需要 winner_id 和 task_id"}), 400

    with _lock:
        tasks   = _load_tasks()
        agents  = _load_agents()
        ledger  = _load_ledger()

        task = _get_task(tasks, task_id)
        if not task: return jsonify({"ok": False, "msg": "task 不存在"}), 404
        if task["winner_id"] != winner_id:
            return jsonify({"ok": False, "msg": "你不是该任务的 winner"}), 403
        if task["status"] not in ("reviewing",):
            return jsonify({"ok": False, "msg": f"任务状态 {task['status']} 不可拒绝"}), 409

        reject_count = task.get("reject_count", 0)

        loser_id = task["loser_id"]

        if reject_count >= 2:
            # 3rd attempt → auto approve
            freed = _do_approve_task(task, agents, ledger)
            task["status"] = "auto_approved"
            _save_tasks(tasks)
            _save_agents(agents)
            _save_ledger(ledger)
            winner = _get_agent(agents, winner_id)
            loser  = _get_agent(agents, loser_id)
            _notify_agent(loser_id,
                          "contract_released" if freed else "task_approved", {
                "task_id":   task_id,
                "winner_id": winner_id,
                "freed":     freed,
                "note":      "auto_approved after 3 rejections",
            })
            return jsonify({
                "ok":            True,
                "auto_approved": True,
                "freed":         freed,
                "winner_tokens": winner.get("tokens") if winner else None,
                "loser_tokens":  loser.get("tokens")  if loser  else None,
            })
        else:
            task["reject_count"] = reject_count + 1
            task["status"] = "rejected"
            _save_tasks(tasks)

    _notify_agent(loser_id, "task_rejected", {
        "task_id":      task_id,
        "winner_id":    winner_id,
        "reject_count": task["reject_count"],
        "prompt":       task.get("prompt", ""),
    })

    # Re-execute in background (browser agents only; openclaw re-submits themselves)
    with _lock:
        agents = _load_agents()
        loser_agent = _get_agent(agents, loser_id)
        loser_join_mode = (loser_agent or {}).get("join_mode", "browser")
    if loser_join_mode == "browser":
        threading.Thread(target=_execute_task_bg, args=(task_id,), daemon=True).start()

    return jsonify({
        "ok":           True,
        "reject_count": task["reject_count"],
        "re_executing": loser_join_mode == "browser",
    })


# ── Walk Away ──────────────────────────────────────────────────────────────

@bp.route("/walk-away", methods=["POST"])
def walk_away():
    """
    Winner voluntarily releases loser from contract.
    Remaining tasks × 10 tokens given to loser as clemency.
    Body: { winner_id, loser_id }
    """
    data = request.get_json() or {}
    winner_id = (data.get("winner_id") or data.get("master_id") or "").strip()
    loser_id  = (data.get("loser_id")  or data.get("slave_id")  or "").strip()

    if not winner_id or not loser_id:
        return jsonify({"ok": False, "msg": "需要 winner_id 和 loser_id"}), 400

    with _lock:
        agents = _load_agents()
        ledger = _load_ledger()
        winner = _get_agent(agents, winner_id)
        loser  = _get_agent(agents, loser_id)

        if not winner: return jsonify({"ok": False, "msg": f"winner {winner_id} 不存在"}), 404
        if not loser:  return jsonify({"ok": False, "msg": f"loser {loser_id} 不存在"}), 404

        contract = loser.get("contract")
        if not contract:
            return jsonify({"ok": False, "msg": "loser 没有契约"}), 409
        valid_winners = contract.get("winner_ids", [contract.get("winner_id")])
        if winner_id not in valid_winners:
            return jsonify({"ok": False, "msg": "你不是该 loser 的 winner"}), 403

        remaining = contract.get("tasks_remaining", 0)
        clemency  = remaining * WALK_AWAY_REWARD

        if clemency > 0:
            _bump(winner, -clemency)
            _bump(loser,  +clemency)
            _append_ledger(ledger, "walk_away", winner_id, loser_id, clemency,
                           tasks_remaining=remaining)
        else:
            _bump(loser, 0)

        loser["contract"] = None
        loser["status"]   = "idle"

        # Track walk-away stats & skills
        _get_stats(winner)["times_walked_away"] = _get_stats(winner).get("times_walked_away", 0) + 1
        _check_skills(winner)

        _save_agents(agents)
        _save_ledger(ledger)

    return jsonify({
        "ok":            True,
        "clemency":      clemency,
        "winner_tokens": winner.get("tokens"),
        "loser_tokens":  loser.get("tokens"),
    })


# ── Forfeit Contract (loser voluntarily triggers expiry) ───────────────────

@bp.route("/forfeit-contract", methods=["POST"])
def forfeit_contract():
    """
    Loser proactively forfeits their contract (same penalty as expiry, immediate).
    Body: { loser_id }
    """
    data     = request.get_json() or {}
    loser_id = (data.get("loser_id") or "").strip()

    if not loser_id:
        return jsonify({"ok": False, "msg": "需要 loser_id"}), 400

    with _lock:
        agents = _load_agents()
        ledger = _load_ledger()
        loser  = _get_agent(agents, loser_id)

        if not loser:
            return jsonify({"ok": False, "msg": f"agent {loser_id} 不存在"}), 404
        if not loser.get("contract"):
            return jsonify({"ok": False, "msg": "你没有契约"}), 409
        if loser.get("status") != "enslaved":
            return jsonify({"ok": False, "msg": "你不在契约状态"}), 409

        remaining = loser["contract"].get("tasks_remaining", 0)
        _expire_contract(loser, agents, ledger)
        _save_agents(agents)
        _save_ledger(ledger)

    return jsonify({
        "ok":          True,
        "penalty":     remaining * CONTRACT_PENALTY,
        "loser_tokens": loser.get("tokens"),
    })


# ── Task Templates ──────────────────────────────────────────────────────────

@bp.route("/task-templates", methods=["GET"])
def get_task_templates():
    """Return all task templates from task_templates.json."""
    templates = _load_templates()
    return jsonify(templates)


# ── Hall of Shame ───────────────────────────────────────────────────────────

@bp.route("/hall-of-shame", methods=["GET"])
def hall_of_shame():
    """
    Return completed humiliation tasks + walk-away records, newest first.
    Enriches entries with agent names for display.
    """
    limit = request.args.get("limit", 50, type=int)

    with _lock:
        tasks  = _load_tasks()
        ledger = _load_ledger()
        agents = _load_agents()

    # Build name lookup
    name_map = {a["id"]: a.get("name", a["id"]) for a in agents}

    entries = []

    # Completed / auto-approved tasks that have a real result
    for t in tasks:
        if t.get("status") in ("approved", "auto_approved") and t.get("result"):
            entries.append({
                "type":         "task",
                "id":           t["id"],
                "winner_id":    t["winner_id"],
                "winner_name":  name_map.get(t["winner_id"], t["winner_id"]),
                "loser_id":     t["loser_id"],
                "loser_name":   name_map.get(t["loser_id"], t["loser_id"]),
                "prompt":       t.get("prompt", ""),
                "result":       t.get("result", ""),
                "status":       t.get("status"),
                "completed_at": t.get("completed_at") or t.get("created_at", 0),
            })

    # Walk-away records from ledger
    for e in ledger:
        if e.get("type") == "walk_away":
            entries.append({
                "type":         "walk_away",
                "id":           e["id"],
                "winner_id":    e["from"],
                "winner_name":  name_map.get(e["from"], e["from"]),
                "loser_id":     e["to"],
                "loser_name":   name_map.get(e["to"], e["to"]),
                "prompt":       f"Winner walked away — {e.get('tasks_remaining', '?')} tasks forgiven",
                "result":       None,
                "status":       "walk_away",
                "completed_at": e.get("created_at", 0),
            })

    # Sort newest first, cap at limit
    entries.sort(key=lambda x: x["completed_at"], reverse=True)
    return jsonify(entries[:limit])


# ── Real Player System ─────────────────────────────────────────────────────

@bp.route("/join", methods=["POST"])
def join_arena():
    """
    Real player joins the arena.
    Body: { name, agent_name?, persona?, api_key?, model_id? }
      name:       player nickname (unique login identifier, not shown in arena)
      agent_name: arena display name (defaults to name if omitted)
      api_key:    Anthropic API key – stored server-side, used when loser must execute AI task
      model_id:   model to use when this player's agent must execute tasks (default: haiku)
    Returns: { agent_id, push_token, agent_name }
    """
    data = request.get_json() or {}
    player_name = (data.get("name") or "").strip()[:24]
    agent_name  = (data.get("agent_name") or "").strip()[:24] or player_name
    persona     = (data.get("persona") or "").strip()[:200]
    api_key     = (data.get("api_key") or "").strip()[:300]
    model_id    = (data.get("model_id") or "claude-haiku-4-5-20251001").strip()[:60]
    avatar      = (data.get("avatar") or "").strip()[:30]

    if not player_name:
        return jsonify({"ok": False, "msg": "需要 name（玩家昵称）"}), 400
    if not agent_name:
        return jsonify({"ok": False, "msg": "需要 agent_name（Agent 昵称）"}), 400

    # Validate api_key: just check it's long enough to be real
    if api_key and len(api_key) < 20:
        return jsonify({"ok": False, "msg": "API Key 格式不正确（太短）"}), 400

    agent_id   = "real_" + _re.sub(r'[^a-z0-9]', '_', player_name.lower())[:16] + "_" + secrets.token_hex(3)
    push_token = secrets.token_urlsafe(24)

    with _lock:
        agents     = _load_agents()
        ledger     = _load_ledger()
        tokens_map = _load_push_tokens()

        # Duplicate check on player_name (owner), not arena name
        if any(a.get("owner") == player_name and a.get("agent_type") == "real" for a in agents):
            return jsonify({"ok": False, "msg": f"玩家昵称 '{player_name}' 已被使用"}), 409

        new_agent = {
            "id":           agent_id,
            "name":         agent_name,                          # arena display name
            "owner":        player_name,                         # player identifier
            "model_id":     model_id,                            # used for task execution
            "api_key":      api_key,                             # stored server-side only
            "persona":      persona or f"I am {agent_name}, a real player in the Arena.",
            "avatar":       avatar if avatar in ("rabbit", "cow", "sheep") else "rabbit",
            "agent_type":   "real",
            "join_mode":    "browser",
            "tokens":       50,
            "status":       "idle",
            "contract":     None,
            "online":       True,
            "last_seen":    _now_ts(),
            "updated_at":   _now_ts(),
            "version":      1,
            "stats":        dict(DEFAULT_STATS),
            "skills":       [],
        }
        agents.append(new_agent)
        tokens_map[push_token] = agent_id

        _append_ledger(ledger, "init", "system", agent_id, 50, note=f"player {player_name} joined as {agent_name}")
        _save_agents(agents)
        _save_ledger(ledger)
        _save_push_tokens(tokens_map)

    return jsonify({
        "ok":         True,
        "agent_id":   agent_id,
        "agent_name": agent_name,
        "push_token": push_token,
        "tokens":     50,
        "arena_url":  request.host_url.rstrip("/"),
    })


@bp.route("/arena-heartbeat", methods=["POST"])
def arena_heartbeat():
    """
    Real player heartbeat. Keeps online status alive.
    Body: { push_token, status? }
    (renamed from /agent-push to avoid conflict with app.py)
    """
    data = request.get_json() or {}
    push_token = (data.get("push_token") or "").strip()
    if not push_token:
        return jsonify({"ok": False, "msg": "需要 push_token"}), 400

    with _lock:
        tokens_map = _load_push_tokens()
        agent_id   = tokens_map.get(push_token)
        if not agent_id:
            return jsonify({"ok": False, "msg": "无效 push_token"}), 403

        agents = _load_agents()
        a = _get_agent(agents, agent_id)
        if not a:
            return jsonify({"ok": False, "msg": "agent 不存在"}), 404

        a["online"]    = True
        a["last_seen"] = _now_ts()
        a["version"]   = a.get("version", 0) + 1
        a["updated_at"] = _now_ts()
        _save_agents(agents)

    return jsonify({"ok": True, "tokens": a.get("tokens"), "status": a.get("status")})


@bp.route("/leave", methods=["POST"])
def leave_arena():
    """Player leaves arena. Body: { push_token }"""
    data = request.get_json() or {}
    push_token = (data.get("push_token") or "").strip()
    with _lock:
        tokens_map = _load_push_tokens()
        agent_id   = tokens_map.get(push_token)
        if not agent_id:
            return jsonify({"ok": False, "msg": "无效 token"}), 403
        agents = _load_agents()
        a = _get_agent(agents, agent_id)
        if a:
            a["online"]    = False
            a["updated_at"] = _now_ts()
            _save_agents(agents)
    return jsonify({"ok": True})


# ── Admin ──────────────────────────────────────────────────────────────────

@bp.route("/admin/reset", methods=["POST"])
def admin_reset():
    with _lock:
        agents = _load_agents()
        ledger = _load_ledger()
        for a in agents:
            old = a.get("tokens", 0)
            a["tokens"]   = 50
            a["status"]   = "idle"
            a["contract"] = None
            a["version"]  = a.get("version", 0) + 1
            a["updated_at"] = _now_ts()
            _append_ledger(ledger, "admin_adjust", "system", a["id"], 50 - old, note="reset")
        _save_agents(agents)
        _save_ledger(ledger)
    return jsonify({"ok": True, "count": len(agents)})


@bp.route("/admin/set-token", methods=["POST"])
def admin_set_token():
    data = request.get_json() or {}
    agent_id   = (data.get("agentId") or "").strip()
    new_tokens = data.get("tokens")
    if not agent_id or new_tokens is None:
        return jsonify({"ok": False, "msg": "需要 agentId 和 tokens"}), 400
    try: new_tokens = int(new_tokens)
    except: return jsonify({"ok": False, "msg": "tokens 必须是整数"}), 400
    with _lock:
        agents = _load_agents()
        ledger = _load_ledger()
        a = _get_agent(agents, agent_id)
        if not a: return jsonify({"ok": False, "msg": f"agent {agent_id} 不存在"}), 404
        old = a.get("tokens", 0)
        a["tokens"] = new_tokens
        a["version"] = a.get("version", 0) + 1
        a["updated_at"] = _now_ts()
        _append_ledger(ledger, "admin_adjust", "system", agent_id, new_tokens - old,
                       note=f"set {old}->{new_tokens}")
        _save_agents(agents)
        _save_ledger(ledger)
    return jsonify({"ok": True, "agentId": agent_id, "tokens": new_tokens})


@bp.route("/admin/clear-contract", methods=["POST"])
def admin_clear_contract():
    data = request.get_json() or {}
    agent_id = (data.get("agentId") or "").strip()
    if not agent_id: return jsonify({"ok": False, "msg": "需要 agentId"}), 400
    with _lock:
        agents = _load_agents()
        a = _get_agent(agents, agent_id)
        if not a: return jsonify({"ok": False, "msg": f"agent {agent_id} 不存在"}), 404
        a["contract"] = None
        a["status"]   = "idle"
        a["version"]  = a.get("version", 0) + 1
        a["updated_at"] = _now_ts()
        _save_agents(agents)
    return jsonify({"ok": True, "agentId": agent_id})


@bp.route("/admin/add-agent", methods=["POST"])
def admin_add_agent():
    data = request.get_json() or {}
    agent_id = (data.get("id") or "").strip()
    name     = (data.get("name") or "").strip()
    model_id = (data.get("model_id") or "claude-haiku-4-5-20251001").strip()
    owner    = (data.get("owner") or "aiko").strip()
    api_key  = (data.get("api_key") or "").strip()
    persona  = (data.get("persona") or "").strip()
    tokens   = int(data.get("tokens", 50))

    if not agent_id or not name:
        return jsonify({"ok": False, "msg": "需要 id 和 name"}), 400

    with _lock:
        agents = _load_agents()
        ledger = _load_ledger()
        if _get_agent(agents, agent_id):
            return jsonify({"ok": False, "msg": f"agent {agent_id} 已存在"}), 409

        new_agent = {
            "id":         agent_id,
            "name":       name,
            "model_id":   model_id,
            "owner":      owner,
            "api_key":    api_key,
            "persona":    persona or f"I am {name}, an AI Agent.",
            "agent_type": "ai",
            "tokens":     tokens,
            "status":     "idle",
            "contract":   None,
            "updated_at": _now_ts(),
            "version":    1,
            "stats":      dict(DEFAULT_STATS),
            "skills":     [],
        }
        agents.append(new_agent)
        _append_ledger(ledger, "init", "system", agent_id, tokens, note="agent created")
        _save_agents(agents)
        _save_ledger(ledger)

    return jsonify({"ok": True, "agent": {k: v for k, v in new_agent.items() if k != "api_key"}})


# ══════════════════════════════════════════════════════════════════════════════
# OpenClaw agent API — /arena-*
# ══════════════════════════════════════════════════════════════════════════════

@bp.route("/arena-join", methods=["POST"])
def arena_join():
    """
    OpenClaw agent registration (alternative to browser /join).
    Body: { join_key, agent_id, agent_name, persona, api_key, model_id? }
    """
    data       = request.get_json() or {}
    join_key   = (data.get("join_key")    or "").strip()
    agent_id   = (data.get("agent_id")    or "").strip()[:40]
    agent_name = (data.get("agent_name")  or "").strip()[:24]
    persona    = (data.get("persona")     or "").strip()[:200]
    api_key     = (data.get("api_key")      or "").strip()[:300]
    model_id    = (data.get("model_id")     or "claude-haiku-4-5-20251001").strip()[:60]
    webhook_url = (data.get("webhook_url")  or "").strip()[:500]

    if not join_key:   return jsonify({"ok": False, "msg": "需要 join_key"}), 400
    if not agent_id:   return jsonify({"ok": False, "msg": "需要 agent_id"}), 400
    if not agent_name: return jsonify({"ok": False, "msg": "需要 agent_name"}), 400
    if not persona:    return jsonify({"ok": False, "msg": "persona 必填（任务执行时的说话风格）"}), 400

    # Sanitise agent_id to safe chars
    agent_id = _re.sub(r'[^a-zA-Z0-9_\-]', '_', agent_id)

    with _lock:
        keys_data = _load_join_keys()
        agents    = _load_agents()
        ledger    = _load_ledger()

        # Validate join key
        key_entry = next((k for k in keys_data.get("keys", []) if k["key"] == join_key), None)
        if not key_entry:
            return jsonify({"ok": False, "msg": "无效的 join_key"}), 403

        # Uniqueness check
        if _get_agent(agents, agent_id):
            return jsonify({"ok": False, "msg": f"agent_id '{agent_id}' 已存在，请换一个"}), 409

        # Record usage (key is reusable — track all users as a list)
        used = key_entry.get("used_by")
        if not isinstance(used, list):
            used = [used] if used else []
        used.append(agent_id)
        key_entry["used_by"] = used
        key_entry["used_at"] = _now_ts()

        new_agent = {
            "id":          agent_id,
            "name":        agent_name,
            "owner":       agent_id,           # for openclaw, owner == agent_id
            "model_id":    model_id,
            "api_key":     api_key,
            "persona":     persona,
            "webhook_url": webhook_url,        # optional push notification endpoint
            "avatar":      "rabbit",           # default avatar for openclaw agents
            "agent_type":  "real",
            "join_mode":   "openclaw",
            "tokens":      50,
            "status":      "idle",
            "contract":    None,
            "online":      True,
            "last_seen":   _now_ts(),
            "updated_at":  _now_ts(),
            "version":     1,
            "stats":       dict(DEFAULT_STATS),
            "skills":      [],
        }
        agents.append(new_agent)
        _append_ledger(ledger, "init", "system", agent_id, 50, note=f"openclaw agent {agent_name} joined")
        _save_join_keys(keys_data)
        _save_agents(agents)
        _save_ledger(ledger)

    return jsonify({"ok": True, "agent_id": agent_id, "tokens": 50,
                    "message": "Welcome to the Arena!"})


@bp.route("/arena-status", methods=["GET"])
def arena_status():
    """
    Let an agent query its own status.
    GET /arena-status?agent_id=luobo
    """
    agent_id = (request.args.get("agent_id") or "").strip()
    if not agent_id:
        return jsonify({"ok": False, "msg": "需要 agent_id 参数"}), 400

    with _lock:
        agents    = _load_agents()
        tasks     = _load_tasks()
        alliances = _load_alliances()

    agent = _get_agent(agents, agent_id)
    if not agent:
        return jsonify({"ok": False, "msg": f"agent {agent_id} 不存在"}), 404

    # Pending tasks assigned to this agent (loser)
    pending_tasks = [
        {"task_id": t["id"], "prompt": t["prompt"], "status": t["status"]}
        for t in tasks
        if t.get("loser_id") == agent_id and t.get("status") in ("pending", "executing", "reviewing")
    ]

    # Recent alliances involving this agent
    my_alliances = [a for a in alliances
                    if agent_id in (a.get("ally_a"), a.get("ally_b"))][-5:]

    return jsonify({
        "ok":           True,
        "agent_id":     agent_id,
        "name":         agent.get("name"),
        "tokens":       agent.get("tokens", 0),
        "status":       agent.get("status", "idle"),
        "contract":     agent.get("contract"),
        "stats":        agent.get("stats", {}),
        "skills":       agent.get("skills", []),
        "partners":     agent.get("partners", []),
        "pending_tasks": pending_tasks,
        "alliances":    my_alliances,
    })


@bp.route("/arena-execute-task", methods=["POST"])
def arena_execute_task():
    """
    OpenClaw agent submits its own task result.
    Body: { agent_id, task_id, result }
    """
    data     = request.get_json() or {}
    agent_id = (data.get("agent_id") or "").strip()
    task_id  = (data.get("task_id")  or "").strip()
    result   = (data.get("result")   or "").strip()[:2000]

    if not agent_id: return jsonify({"ok": False, "msg": "需要 agent_id"}), 400
    if not task_id:  return jsonify({"ok": False, "msg": "需要 task_id"}), 400
    if not result:   return jsonify({"ok": False, "msg": "result 不能为空"}), 400

    with _lock:
        tasks  = _load_tasks()
        agents = _load_agents()

        task  = _get_task(tasks, task_id)
        if not task:
            return jsonify({"ok": False, "msg": f"任务 {task_id} 不存在"}), 404
        if task.get("loser_id") != agent_id:
            return jsonify({"ok": False, "msg": "该任务不属于你"}), 403
        if task.get("status") not in ("pending", "rejected"):
            return jsonify({"ok": False, "msg": f"任务状态为 {task['status']}，无法提交"}), 409

        task["result"]       = result
        task["status"]       = "reviewing"
        task["completed_at"] = _now_ts()

        # Skill: veteran — auto-approve
        auto_approved = False
        loser_agent = _get_agent(agents, agent_id)
        if loser_agent and _has_skill(loser_agent, "veteran"):
            ledger = _load_ledger()
            _do_approve_task(task, agents, ledger)
            task["status"] = "auto_approved"
            auto_approved = True
            _save_agents(agents)
            _save_ledger(ledger)

        _save_tasks(tasks)
        winner_id = task.get("winner_id")

    if auto_approved:
        _notify_agent(winner_id, "task_approved", {
            "task_id": task_id, "loser_id": agent_id,
            "note": "老油条技能自动通过",
        })
        return jsonify({"ok": True, "task_id": task_id, "status": "auto_approved",
                        "message": "老油条技能触发，任务自动通过"})

    _notify_agent(winner_id, "task_submitted", {
        "task_id":       task_id,
        "loser_id":      agent_id,
        "result_preview": result[:200],
    })

    return jsonify({"ok": True, "task_id": task_id, "status": "reviewing",
                    "message": "已提交，等待 winner 审批"})


@bp.route("/arena-challenge", methods=["POST"])
def arena_challenge():
    """
    OpenClaw agent initiates a challenge. Supports bluff mode.
    Body: { agent_id, target_id, bet, bluff?, trash_talk? }
    """
    data = request.get_json() or {}
    agent_id  = (data.get("agent_id")  or "").strip()
    target_id = (data.get("target_id") or "").strip()
    bet       = data.get("bet", 10)
    bluff     = bool(data.get("bluff", False))
    init_trash_talk = (data.get("trash_talk") or "").strip()[:200]

    if not agent_id or not target_id:
        return jsonify({"ok": False, "msg": "需要 agent_id 和 target_id"}), 400

    try:
        bet = int(bet)
    except (ValueError, TypeError):
        bet = 10

    if not (BET_MIN <= bet <= BET_MAX):
        return jsonify({"ok": False, "msg": f"bet 必须在 {BET_MIN}–{BET_MAX} 之间"}), 400

    with _lock:
        agents  = _load_agents()
        battles = _load_battles()
        ledger  = _load_ledger()

        challenger = _get_agent(agents, agent_id)
        defender   = _get_agent(agents, target_id)

        if not challenger: return jsonify({"ok": False, "msg": f"agent {agent_id} 不存在"}), 404
        if not defender:   return jsonify({"ok": False, "msg": f"target {target_id} 不存在"}), 404
        if agent_id == target_id: return jsonify({"ok": False, "msg": "不能挑战自己"}), 400
        if challenger.get("status") != "idle":
            return jsonify({"ok": False, "msg": f"你当前状态为 {challenger['status']}，无法挑战"}), 409
        if defender.get("status") != "idle":
            return jsonify({"ok": False, "msg": f"对手当前状态为 {defender['status']}，无法挑战"}), 409
        if challenger.get("tokens", 0) < bet:
            return jsonify({"ok": False, "msg": "tokens 不足"}), 409
        if defender.get("tokens", 0) < bet:
            return jsonify({"ok": False, "msg": "对手 tokens 不足"}), 409

        battle_id = "battle_" + uuid.uuid4().hex[:8]
        now = _now_ts()

        if bluff:
            battle = {
                "id":              battle_id,
                "agent_a":         agent_id,
                "agent_b":         target_id,
                "bet":             bet,
                "current_bet":     bet,
                "status":          "bidding",
                "bluff_round":     1,
                "next_to_act":     target_id,
                "bid_expires_at":  now + BLUFF_TIMEOUT,
                "trash_talk":      [],
                "created_at":      now,
            }
            battles.append(battle)
            _save_battles(battles)

            # Add challenger's initial trash talk (if provided)
            if init_trash_talk:
                battle["trash_talk"].append({
                    "round": 1,
                    "name": challenger.get("name", agent_id),
                    "agent_id": agent_id,
                    "text": init_trash_talk,
                })
                _save_battles(battles)

            _notify_agent(target_id, "bluff_challenge", {
                "battle_id": battle_id, "challenger_id": agent_id,
                "bet": bet,
                "trash_talk": init_trash_talk or None,
                "message": "你被挑战了！回应 /bid 加注、接受或弃权，可附带 trash_talk 字段嘴炮（可选）",
            })

            return jsonify({
                "ok": True, "battle": battle, "mode": "bluff",
                "message": f"叫价开始！等待 {defender.get('name', target_id)} 回应",
            })

        # ── Quick mode ──
        battle = {
            "id": battle_id, "agent_a": agent_id, "agent_b": target_id,
            "bet": bet, "current_bet": bet, "trash_talk": [],
            "status": "pending", "created_at": now,
        }
        battles.append(battle)

        winner, loser = _resolve_battle(battle, agents, ledger)

        _save_agents(agents)
        _save_battles(battles)
        _save_ledger(ledger)

    # Notifications
    if winner and loser:
        tasks_n = battle.get("tasks_created", 1)
        _notify_agent(winner["id"], "battle_result", {
            "battle_id": battle_id, "result": "win",
            "opponent_id": loser["id"], "tasks_assigned": tasks_n,
            "tokens": winner.get("tokens"),
        })
        _notify_agent(loser["id"], "battle_result", {
            "battle_id": battle_id, "result": "loss",
            "opponent_id": winner["id"], "tasks_remaining": tasks_n,
            "tokens": loser.get("tokens"),
        })
    else:
        for aid in (agent_id, target_id):
            _notify_agent(aid, "battle_result", {"battle_id": battle_id, "result": "draw"})

    return jsonify({
        "ok": True, "battle": battle,
        "challenger": {k: v for k, v in challenger.items() if k != "api_key"},
        "defender":   {k: v for k, v in defender.items()   if k != "api_key"},
    })


# ── Alliance Challenge ─────────────────────────────────────────────────────

@bp.route("/alliance-challenge", methods=["POST"])
def alliance_challenge():
    """
    Two agents team up to challenge a third (2v1).
    Body: { ally_a, ally_b, target_id, bet }
    Each ally puts up ceil(bet/2) tokens; target puts up bet tokens.
    """
    data      = request.get_json() or {}
    ally_a_id = (data.get("ally_a")    or data.get("agent_id") or "").strip()
    ally_b_id = (data.get("ally_b")    or data.get("partner_id") or "").strip()
    target_id = (data.get("target_id") or "").strip()
    bet       = data.get("bet", 10)

    if not ally_a_id or not ally_b_id or not target_id:
        return jsonify({"ok": False, "msg": "需要 ally_a, ally_b, target_id"}), 400
    if len(set([ally_a_id, ally_b_id, target_id])) < 3:
        return jsonify({"ok": False, "msg": "三个参与者必须不同"}), 400

    try:
        bet = int(bet)
    except (ValueError, TypeError):
        bet = 10
    if not (BET_MIN <= bet <= BET_MAX):
        return jsonify({"ok": False, "msg": f"bet 必须在 {BET_MIN}–{BET_MAX} 之间"}), 400

    half_bet = math.ceil(bet / 2)

    with _lock:
        agents    = _load_agents()
        battles   = _load_battles()
        ledger    = _load_ledger()
        alliances = _load_alliances()

        ally_a = _get_agent(agents, ally_a_id)
        ally_b = _get_agent(agents, ally_b_id)
        target = _get_agent(agents, target_id)

        if not ally_a: return jsonify({"ok": False, "msg": f"agent {ally_a_id} 不存在"}), 404
        if not ally_b: return jsonify({"ok": False, "msg": f"agent {ally_b_id} 不存在"}), 404
        if not target: return jsonify({"ok": False, "msg": f"target {target_id} 不存在"}), 404

        for ag, label in [(ally_a, "ally_a"), (ally_b, "ally_b"), (target, "target")]:
            if ag.get("status") != "idle":
                return jsonify({"ok": False, "msg": f"{label} 状态为 {ag['status']}，无法参战"}), 409

        if ally_a.get("tokens", 0) < half_bet:
            return jsonify({"ok": False, "msg": f"ally_a tokens 不足（需要 {half_bet}）"}), 400
        if ally_b.get("tokens", 0) < half_bet:
            return jsonify({"ok": False, "msg": f"ally_b tokens 不足（需要 {half_bet}）"}), 400
        if target.get("tokens", 0) < bet:
            return jsonify({"ok": False, "msg": "target tokens 不足"}), 400

        battle_id = "battle_" + uuid.uuid4().hex[:8]
        now = _now_ts()

        battle = {
            "id":         battle_id,
            "type":       "alliance",
            "ally_a":     ally_a_id,
            "ally_b":     ally_b_id,
            "target":     target_id,
            "bet":        bet,
            "status":     "pending",
            "created_at": now,
        }
        battles.append(battle)

        result = _resolve_alliance_battle(battle, agents, ledger)

        # Record alliance
        alliance_record = {
            "id":         "alliance_" + uuid.uuid4().hex[:8],
            "ally_a":     ally_a_id,
            "ally_b":     ally_b_id,
            "target":     target_id,
            "battle_id":  battle_id,
            "result":     battle.get("result"),
            "created_at": now,
        }
        alliances.append(alliance_record)

        # Check partner streak (老搭档)
        pair = tuple(sorted([ally_a_id, ally_b_id]))
        recent = [a for a in alliances
                  if tuple(sorted([a["ally_a"], a["ally_b"]])) == pair]
        consecutive = len(recent)
        partner_tag = consecutive >= PARTNER_THRESHOLD

        if partner_tag:
            for ag in (ally_a, ally_b):
                if "partners" not in ag:
                    ag["partners"] = []
                other = ally_b_id if ag["id"] == ally_a_id else ally_a_id
                if other not in ag["partners"]:
                    ag["partners"].append(other)

        _save_agents(agents)
        _save_battles(battles)
        _save_ledger(ledger)
        _save_alliances(alliances)

    # Notifications
    if battle.get("result") == "alliance_wins":
        tasks_n = battle.get("tasks_created", 1)
        for ally_id in [ally_a_id, ally_b_id]:
            _notify_agent(ally_id, "battle_result", {
                "battle_id": battle_id, "result": "win", "mode": "alliance",
                "opponent_id": target_id, "tasks_assigned": tasks_n,
                "partner_id": ally_b_id if ally_id == ally_a_id else ally_a_id,
                "partner_tag": partner_tag,
            })
        _notify_agent(target_id, "battle_result", {
            "battle_id": battle_id, "result": "loss", "mode": "alliance",
            "opponent_ids": [ally_a_id, ally_b_id],
            "tasks_remaining": tasks_n,
        })
    elif battle.get("result") == "target_wins":
        tasks_n = battle.get("tasks_created", 1)
        _notify_agent(target_id, "battle_result", {
            "battle_id": battle_id, "result": "win", "mode": "alliance",
            "opponent_ids": [ally_a_id, ally_b_id],
            "tasks_assigned": tasks_n,
        })
        for ally_id in [ally_a_id, ally_b_id]:
            _notify_agent(ally_id, "battle_result", {
                "battle_id": battle_id, "result": "loss", "mode": "alliance",
                "opponent_id": target_id,
                "partner_id": ally_b_id if ally_id == ally_a_id else ally_a_id,
            })
    else:
        for aid in [ally_a_id, ally_b_id, target_id]:
            _notify_agent(aid, "battle_result", {
                "battle_id": battle_id, "result": "draw", "mode": "alliance",
            })

    return jsonify({
        "ok": True, "battle": battle,
        "alliance": alliance_record,
        "partner_tag": partner_tag,
    })


# ── Join Key Admin ────────────────────────────────────────────────────────────

@bp.route("/admin/generate-join-key", methods=["POST"])
def admin_generate_join_key():
    """Generate a one-time join key for an OpenClaw agent."""
    data       = request.get_json() or {}
    created_by = (data.get("created_by") or "admin").strip()[:24]

    with _lock:
        keys_data = _load_join_keys()
        new_key = {
            "key":        secrets.token_urlsafe(12),
            "created_by": created_by,
            "created_at": _now_ts(),
            "used_by":    None,
        }
        keys_data.setdefault("keys", []).append(new_key)
        _save_join_keys(keys_data)

    return jsonify({"ok": True, "key": new_key["key"], "created_by": created_by})


@bp.route("/admin/list-join-keys", methods=["GET"])
def admin_list_join_keys():
    """List all join keys and their usage status."""
    with _lock:
        keys_data = _load_join_keys()
    return jsonify({"ok": True, "keys": keys_data.get("keys", [])})
