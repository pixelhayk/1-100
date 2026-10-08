import json, os, random, re, secrets, threading, time
from functools import wraps
from flask import Flask, jsonify, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(HERE, "users.json")
NAME_RE = re.compile(r"^[\w .\-]{2,16}$")
app = Flask(__name__, static_folder=os.path.join(HERE, "static"))
LOCK = threading.Lock()
users, tokens, games = {}, {}, {}   # users: lower-name -> data; tokens: token -> lower-name
ACTIVE = ("invited", "secrets", "play")


def load():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, encoding="utf-8") as f:
            for k, v in json.load(f).items():
                users[k] = {"name": v["name"], "token": v["token"], "seen": 0, "game": None, "invites": []}
                tokens[v["token"]] = k


def save():  # names are kept forever
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump({k: {"name": u["name"], "token": u["token"]} for k, u in users.items()}, f, ensure_ascii=False)


load()


def err(msg, code=400):
    return jsonify(error=msg), code


def need(f):
    @wraps(f)
    def w():
        d = request.get_json(silent=True) or {}
        tok = d.get("token") or request.args.get("token", "")
        with LOCK:
            k = tokens.get(tok)
            if not k:
                return err("auth", 401)
            u = users[k]
            u["seen"] = time.time()
            sweep()
            return f(k, u, d)
    return w


def sweep():
    now = time.time()
    for gid, g in list(games.items()):
        if g["status"] == "invited" and now - g["t"] > 120:
            g["status"] = "expired"
            users[g["p"][1]]["invites"] = [x for x in users[g["p"][1]]["invites"] if x != gid]
        if now - g["t"] > 10800:
            del games[gid]


def cur(u):
    g = games.get(u["game"]) if u["game"] else None
    return g


def busy(u):
    g = cur(u)
    return bool(g and g["status"] in ACTIVE)


def drop_invite(g):
    t = users[g["p"][1]]
    t["invites"] = [x for x in t["invites"] if x != g["id"]]


def view(g, k):
    d = {"gid": g["id"], "status": g["status"], "me": g["p"].index(k),
         "names": [users[x]["name"] for x in g["p"]], "ready": [s is not None for s in g["sec"]],
         "turn": g["turn"], "moves": g["moves"], "rng": g["rng"], "winner": g["winner"]}
    if g["status"] == "done":
        d["sec"] = g["sec"]
    return d


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.post("/api/register")
def register():
    d = request.get_json(silent=True) or {}
    name, token = str(d.get("name", "")).strip(), str(d.get("token", ""))
    if not NAME_RE.match(name):
        return err("Имя: 2–16 символов — буквы, цифры, пробел, _ - .")
    k = name.lower()
    with LOCK:
        u = users.get(k)
        if u and u["token"] != token:
            return err("Это имя уже занято", 409)
        if not u:
            if len(token) < 16 or token in tokens:
                token = secrets.token_hex(16)
            u = users[k] = {"name": name, "token": token, "seen": 0, "game": None, "invites": []}
            tokens[token] = k
            save()
        u["seen"] = time.time()
        return jsonify(ok=True, name=u["name"], token=u["token"])


@app.get("/api/state")
@need
def state(k, u, d):
    inv = []
    for gid in u["invites"]:
        g = games.get(gid)
        if g and g["status"] == "invited":
            inv.append({"gid": gid, "from": users[g["p"][0]]["name"]})
    g = cur(u)
    return jsonify(invites=inv, game=view(g, k) if g else None)


@app.post("/api/invite")
@need
def invite(k, u, d):
    tk = str(d.get("to", "")).strip().lower()
    t = users.get(tk)
    if not t:
        return err("Игрок с таким именем не найден")
    if tk == k:
        return err("Нельзя позвать самого себя")
    if time.time() - t["seen"] > 15:
        return err("Этот игрок сейчас не в сети")
    if busy(u) or busy(t):
        return err("Один из игроков уже в игре")
    gid = secrets.token_hex(6)
    g = games[gid] = {"id": gid, "p": [k, tk], "status": "invited", "sec": [None, None], "turn": 0,
                      "moves": [], "rng": [[1, 100], [1, 100]], "winner": None, "t": time.time()}
    u["game"] = gid
    t["invites"].append(gid)
    return jsonify(game=view(g, k))


@app.post("/api/respond")
@need
def respond(k, u, d):
    g = games.get(d.get("gid"))
    if not g or g["status"] != "invited" or g["p"][1] != k:
        return err("Приглашение больше не действует")
    drop_invite(g)
    g["t"] = time.time()
    if d.get("accept") and not busy(u):
        g["status"], u["game"] = "secrets", g["id"]
        return jsonify(game=view(g, k))
    g["status"] = "declined"
    return jsonify(game=None)


def mine(k, d, status):
    g = games.get(d.get("gid"))
    if not g or k not in g["p"] or g["status"] != status:
        return None, err("Игра уже не активна", 409)
    return g, None


def num(d):
    n = d.get("n")
    return n if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= 100 else None


@app.post("/api/secret")
@need
def secret(k, u, d):
    g, e = mine(k, d, "secrets")
    n = num(d)
    if e or n is None:
        return e or err("Число от 1 до 100")
    g["sec"][g["p"].index(k)] = n
    g["t"] = time.time()
    if all(s is not None for s in g["sec"]):
        g["status"], g["turn"] = "play", random.randint(0, 1)
    return jsonify(game=view(g, k))


@app.post("/api/guess")
@need
def guess(k, u, d):
    g, e = mine(k, d, "play")
    n = num(d)
    if e or n is None:
        return e or err("Число от 1 до 100")
    me = g["p"].index(k)
    if g["turn"] != me:
        return err("Сейчас не твой ход", 409)
    opp, s = 1 - me, g["sec"][1 - me]
    res = 0 if n == s else (1 if s > n else -1)
    g["moves"].append({"by": me, "n": n, "res": res})
    g["t"] = time.time()
    if res == 0:
        g["status"], g["winner"] = "done", me
    else:
        r = g["rng"][opp]
        if res > 0:
            r[0] = max(r[0], n + 1)
        else:
            r[1] = min(r[1], n - 1)
        g["turn"] = opp
    return jsonify(game=view(g, k))


@app.post("/api/leave")
@need
def leave(k, u, d):
    g = cur(u)
    if g and g["id"] == d.get("gid"):
        if g["status"] == "invited":
            g["status"] = "cancelled"
            drop_invite(g)
        elif g["status"] in ("secrets", "play"):
            g["status"] = "abandoned"
        u["game"] = None
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True)
