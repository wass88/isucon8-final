from __future__ import annotations

import os, sys

sys.path.append(os.path.dirname(__file__) + "/vendor")

import contextlib
import datetime
import json
import time
import flask
import MySQLdb
import requests
import redis

from dataclasses import dataclass, asdict
from . import model


# port   = os.environ.get("ISU_APP_PORT", "5000")
dbhost = os.environ.get("ISU_DB_HOST", "127.0.0.1")
dbport = os.environ.get("ISU_DB_PORT", "3306")
dbuser = os.environ.get("ISU_DB_USER", "root")
dbpass = os.environ.get("ISU_DB_PASSWORD", "")
dbname = os.environ.get("ISU_DB_NAME", "isucoin")
public = os.environ.get("ISU_PUBLIC_DIR", "public")

app = flask.Flask(__name__, static_url_path="", static_folder=public)
app.secret_key = "tonymoris"

# ISUCON用初期データの基準時間です
# この時間以降のデータはinitializeで削除されます
base_time = datetime.datetime(2018, 10, 16, 10, 0, 0)

_dbconn = None


def get_dbconn():
    # NOTE: get_dbconn() is not thread safe.  Don't use threaded server.
    global _dbconn

    if _dbconn is None:
        _dbconn = MySQLdb.connect(
            host=dbhost,
            port=int(dbport),
            user=dbuser,
            password=dbpass,
            database=dbname,
            charset="utf8mb4",
            autocommit=True,
        )

    return _dbconn


def _json_default(v):
    if isinstance(v, datetime.datetime):
        return v.strftime("%Y-%m-%dT%H:%M:%S+09:00")

    to_json = getattr(v, "to_json")
    if to_json:
        return to_json()

    raise TypeError(f"Unknown type for json_dumps. {v!r} (type: {type(v)})")


def json_dumps(data, **kwargs):
    return json.dumps(data, default=_json_default, **kwargs)


def jsonify(*args, **kwargs):
    if args and kwargs:
        raise TypeError("jsonify() behavior undefined when passed both args and kwargs")
    if len(args) == 1:
        data = args[0]
    else:
        data = args or kwargs

    return app.response_class(
        json_dumps(data, indent=None, separators=(",", ":")).encode(),
        mimetype="application/json; charset=utf-8",
    )


def error_json(code: int, msg):
    resp = jsonify(code=code, err=str(msg))
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.status_code = code
    return resp


@app.errorhandler(Exception)
def errohandler(err):
    app.logger.exception("FAIL")
    return error_json(500, err)


@app.before_request
def before_request():
    user_id = flask.session.get("user_id")
    if user_id is None:
        flask.g.current_user = None
        return

    user = model.get_user_by_id(get_dbconn(), user_id)
    if user is None:
        flask.session.clear()
        return error_json(404, "セッションが切断されました")

    flask.g.current_user = user


@contextlib.contextmanager
def transaction():
    conn = get_dbconn()
    conn.begin()
    try:
        yield conn
    except:
        conn.rollback()
        raise
    else:
        conn.commit()


@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/initialize", methods=("POST",))
def initialize():
    with transaction() as db:
        model.init_benchmark(db)

    for server in ['isucon1', 'isucon2', 'isucon4']:
        print(f"send request to {server}")
        requests.post(f"http://{server}:5000/initialize_redis", data=flask.request.form)
    requests.post("http://isucon1:5005/initialize")
    
    return jsonify({})

@app.route("/initialize_redis", methods=("POST",))
def initialize_redis():
    _redis().flushdb()
    get_dbconn().ping(True)
    for k in ("bank_endpoint", "bank_appid", "log_endpoint", "log_appid"):
        v = flask.request.form.get(k)
        model.set_setting(k, v)
    return jsonify({})

@app.route("/signup", methods=("POST",))
def signup():
    req = flask.request
    name = req.form["name"]
    bank_id = req.form["bank_id"]
    password = req.form["password"]

    if not (name and bank_id and password):
        return error_json(400, "all parameters are required")

    try:
        with transaction() as db:
            model.signup(db, name, bank_id, password)
    except model.BankUserNotFound as e:
        return error_json(404, e.msg)
    except model.BankUserConflict as e:
        return error_json(409, e.msg)

    return jsonify({})


# Global settings
_redisconn = None
_redispool = None


def _redis():
    # NOTE: get_dbconn() is not thread safe.  Don't use threaded server.
    global _redisconn
    global _redispool

    if _redisconn is None:
        _redispool = redis.ConnectionPool(
            host='localhost',
            port=6379,
            db=0,
        )
        _redisconn = redis.StrictRedis(
            connection_pool=_redispool
        )

    return _redisconn


@app.route("/signin", methods=("POST",))
def signin():
    req = flask.request
    bank_id = req.form["bank_id"]
    password = req.form["password"]

    if not (bank_id and password):
        return error_json(400, "all parameters are required")

    db = get_dbconn()
    try:
        user = model.login(db, bank_id, password)
    except model.UserNotFound as e:
        redis_key = f'signin_fail_{bank_id}'
        fail_count = int(_redis().incr(redis_key))
        if fail_count > 5:
            return error_json(403, 'banned')
        # TODO: 失敗が多いときに403を返すBanの仕様に対応
        return error_json(404, e.msg)

    flask.session["user_id"] = user.id
    return jsonify(id=user.id, name=user.name)


@app.route("/signout", methods=("POST",))
def signout():
    flask.session.clear()
    return jsonify({})


@app.route("/info")
def info():
    res = {}
    db = get_dbconn()
    cursor = flask.request.args.get("cursor")
    last_trade_id = 0
    lt = None

    if cursor:
        try:
            last_trade_id = int(cursor)
        except ValueError as e:
            app.logger.exception(f"failed to parse cursor ({cursor!r})")
        if last_trade_id > 0:
            trade = model.get_trade_by_id(db, last_trade_id)
            if trade:
                lt = trade.created_at

    latest_trade = model.get_latest_trade(db)
    res["cursor"] = latest_trade.id

    user = flask.g.current_user
    if user:
        orders = model.get_orders_by_userid_and_lasttradeid(db, user.id, last_trade_id)
        c = db.cursor()
        c.execute("""
        SELECT
            orders.*,
            user.id as user_id,
            user.bank_id as user_bank_id,
            user.name as user_name,
            user.password as user_password,
            user.created_at as user_created_at,
            trade.id as trade_id,
            trade.amount as trade_amount,
            trade.price as trade_price,
            trade.created_at as trade_created_at
        FROM orders
        INNER JOIN user ON user.id = orders.user_id
        LEFT JOIN trade ON trade.id = orders.trade_id
        WHERE orders.user_id = %s
        AND orders.trade_id IS NOT NULL
        AND orders.trade_id > %s
        ORDER BY orders.created_at ASC
        """,
            (user.id, last_trade_id),
        )
        rows = c.fetchall()
        orders = [model.Order(*r[:8]) for r in rows]
        users = [model.User(*r[8:13]) for r in rows]
        trades = [r[13] and model.Trade(*r[13:]) for r in rows]
        for o, u, t in zip(orders, users, trades):
            o.user = u.to_json()
            if o.trade_id:
                o.trade = asdict(t)

        res["traded_orders"] = orders

    from_t = base_time - datetime.timedelta(seconds=300)
    if lt and lt > from_t:
        from_t = lt.replace(microsecond=0)
    res["chart_by_sec"] = model.get_candlestic_data(db, from_t, "%Y-%m-%d %H:%i:%s")

    from_t = base_time - datetime.timedelta(minutes=300)
    if lt and lt > from_t:
        from_t = lt.replace(second=0, microsecond=0)
    res["chart_by_min"] = model.get_candlestic_data(db, from_t, "%Y-%m-%d %H:%i:00")

    from_t = base_time - datetime.timedelta(hours=48)
    if lt and lt > from_t:
        from_t = lt.replace(minute=0, second=0, microsecond=0)
    res["chart_by_hour"] = model.get_candlestic_data(db, from_t, "%Y-%m-%d %H:00:00")

    lowest_sell_order = model.get_lowest_sell_order(db)
    if lowest_sell_order:
        res["lowest_sell_price"] = lowest_sell_order.price

    highest_buy_order = model.get_highest_buy_order(db)
    if highest_buy_order:
        res["highest_buy_price"] = highest_buy_order.price

    # TODO: trueにするとシェアボタンが有効になるが、アクセスが増えてヤバイので一旦falseにしておく
    res["enable_share"] = False

    resp = jsonify(res)
    return resp


@app.route("/orders")
def orders():
    user = flask.g.current_user
    if user is None:
        return error_json(401, "Not authenticated")

    db = get_dbconn()
    c = db.cursor()
    c.execute("""
        SELECT
            o.*,
            u.id as user_id,
            u.bank_id as user_bank_id,
            u.name as user_name,
            u.password as user_password,
            u.created_at as user_created_at,
            t.id as trade_id,
            t.amount as trade_amount,
            t.price as trade_price,
            t.created_at as trade_created_at
        FROM orders o
        INNER JOIN user u ON u.id = o.user_id
        LEFT JOIN trade t ON t.id = o.trade_id
        WHERE o.user_id = %s
        AND (o.closed_at IS NULL OR o.trade_id IS NOT NULL)
        ORDER BY o.created_at ASC
    """, (user.id,),
    )
    rows = c.fetchall()
    orders = [model.Order(*r[:8]) for r in rows]
    users = [model.User(*r[8:13]) for r in rows]
    trades = [r[13] and model.Trade(*r[13:]) for r in rows]
    for o, u, t in zip(orders, users, trades):
        o.user = u.to_json()
        if o.trade_id:
            o.trade = asdict(t)

    return jsonify(orders)


@app.route("/orders", methods=("POST",))
def add_order():
    user = flask.g.current_user
    if user is None:
        return error_json(401, "Not authenticated")

    amount = int(flask.request.form["amount"])
    price = int(flask.request.form["price"])
    type = flask.request.form["type"]

    try:
        with transaction() as db:
            order = model.add_order(db, type, user.id, amount, price)
    except model.CreditInsufficient as e:
        return error_json(400, e.msg)

    db = get_dbconn()
    trade_chance = model.has_trade_chance_by_order(db, order.id)
    if trade_chance:
        try:
            model.run_trade(db)
        except Exception:  # トレードに失敗してもエラーにはしない
            app.logger.exception("run_trade failed")

    return jsonify(id=order.id)


@app.route("/order/<int:order_id>", methods=("DELETE",))
def delete_order(order_id):
    user = flask.g.current_user
    if user is None:
        return error_json(401, "Not authenticated")

    try:
        with transaction() as db:
            model.delete_order(db, user.id, order_id, "canceled")
    except (model.OrderNotFound, model.OrderAlreadyClosed) as e:
        error_json(404, e.msg)

    return jsonify(id=order_id)
