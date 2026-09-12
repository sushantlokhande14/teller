"""
Meridian Core Servicing (fictional vendor product, "v4.2").

A deliberately old-fashioned, server-rendered member servicing console: a
frameset, table-based layouts, <font> tags, no ids, no test hooks. It stands in
for the kind of vendor back-office app that teller has to drive through the UI.

Runtime faults can be injected through POST /__fault so replays can be exercised
against the exceptional states that happen in production (session expiry,
permission denials, interstitials, slow loads, application errors).
"""
from __future__ import annotations

import copy
import os

from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   session, url_for)

app = Flask(__name__)
app.secret_key = "meridian-dev-only-not-a-real-secret"
app.config["TEMPLATES_AUTO_RELOAD"] = True

USERS = {os.environ.get("MERIDIAN_USER", "operator1"): os.environ.get("MERIDIAN_PASS", "teller!23")}

SEED_MEMBERS = {
    "100234": {
        "name": "Priya Natarajan", "since": "03/14/2011", "ssn_last4": "4821",
        "address": "18 Alder Ct, Fremont CA 94536",
        "accounts": [
            {"number": "100234-S01", "type": "Savings", "status": "Open", "balance": "4,812.55", "available": "4,812.55"},
            {"number": "100234-C01", "type": "Checking", "status": "Open", "balance": "1,203.10", "available": "1,153.10"},
        ],
    },
    "100877": {
        "name": "Marcus Bell", "since": "07/02/2016", "ssn_last4": "0193",
        "address": "902 Willow Ave Apt 4, San Jose CA 95126",
        "accounts": [
            {"number": "100877-S01", "type": "Savings", "status": "Frozen", "balance": "215.00", "available": "0.00"},
        ],
    },
    "101502": {
        "name": "Elena Ruiz", "since": "11/20/2019", "ssn_last4": "7760",
        "address": "5 Harbor Ln, Alameda CA 94501",
        "accounts": [
            {"number": "101502-C01", "type": "Checking", "status": "Open", "balance": "88.40", "available": "88.40"},
            {"number": "101502-S01", "type": "Savings", "status": "Open", "balance": "12,000.00", "available": "12,000.00"},
        ],
    },
}
MEMBERS = copy.deepcopy(SEED_MEMBERS)

PRODUCTS = ["Savings", "Money Market", "Certificate"]

# kind -> "once" | "sticky". Consumed by the next request under /main/.
FAULTS: dict[str, str] = {}
FAULT_KINDS = ("session_expired", "slow", "notice", "error", "permission", "surprise")


def take_fault(kind: str) -> bool:
    mode = FAULTS.get(kind)
    if mode is None:
        return False
    if mode == "once":
        FAULTS.pop(kind, None)
    return True


@app.before_request
def guard():
    path = request.path
    if not path.startswith("/main/"):
        return None
    if "user" not in session:
        return redirect(url_for("login", next=path))
    # Interstitials stay up until acknowledged, so they never go through take_fault().
    if path.startswith(("/main/notice", "/main/surprise")):
        return None
    if take_fault("session_expired"):
        session.clear()
        return redirect(url_for("login", next=path, reason="timeout"))
    if take_fault("permission"):
        return render_template("denied.html", what=path), 403
    if take_fault("error"):
        return render_template("error.html", ref="ORA-01555"), 500
    if take_fault("slow"):
        return render_template("busy.html"), 200
    if "notice" in FAULTS:
        return render_template("notice.html", next=request.full_path.rstrip("?"))
    if "surprise" in FAULTS:
        return render_template("surprise.html", next=request.full_path.rstrip("?"))
    return None


@app.route("/")
def frameset():
    if "user" not in session:
        return redirect(url_for("login"))
    return render_template("frameset.html")


@app.route("/nav")
def nav():
    return render_template("nav.html", user=session.get("user", ""))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    reason = request.args.get("reason")
    if request.method == "POST":
        user = request.form.get("userid", "")
        if USERS.get(user) == request.form.get("password", ""):
            session["user"] = user
            return redirect(request.form.get("next") or "/")
        error = "Invalid user ID or password."
    return render_template("login.html", error=error, reason=reason,
                           next=request.args.get("next", ""))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/main/home")
def home():
    return render_template("home.html", user=session["user"])


@app.route("/main/members")
def members():
    return render_template("members.html", error=None, query="")


@app.route("/main/members/search", methods=["POST"])
def members_search():
    q = request.form.get("memberno", "").strip()
    if not q.isdigit():
        return render_template("members.html", error="Member number must be numeric.", query=q)
    if q not in MEMBERS:
        return render_template("members.html", error=f"No member found for number {q}.", query=q)
    return redirect(url_for("member_detail", member_id=q))


@app.route("/main/members/<member_id>")
def member_detail(member_id: str):
    m = MEMBERS.get(member_id)
    if m is None:
        return render_template("members.html", error=f"No member found for number {member_id}.", query=member_id)
    return render_template("member.html", member_id=member_id, m=m)


@app.route("/main/members/<member_id>/subaccounts/new")
def subaccount_new(member_id: str):
    m = MEMBERS.get(member_id) or abort(404)
    return render_template("subaccount_new.html", member_id=member_id, m=m, products=PRODUCTS,
                           error=None, form={"product": "Savings", "nickname": "", "deposit": "0.00"})


@app.route("/main/members/<member_id>/subaccounts/review", methods=["POST"])
def subaccount_review(member_id: str):
    m = MEMBERS.get(member_id) or abort(404)
    form = {k: request.form.get(k, "").strip() for k in ("product", "nickname", "deposit")}
    error = None
    if form["product"] not in PRODUCTS:
        error = "Select a valid product."
    elif not form["nickname"]:
        error = "Nickname is required."
    else:
        try:
            amount = float(form["deposit"].replace(",", "").replace("$", ""))
            if amount < 0:
                raise ValueError
            form["deposit"] = f"{amount:,.2f}"
        except ValueError:
            error = "Initial deposit must be a dollar amount."
    if error:
        return render_template("subaccount_new.html", member_id=member_id, m=m, products=PRODUCTS,
                               error=error, form=form)
    return render_template("subaccount_review.html", member_id=member_id, m=m, form=form)


@app.route("/main/members/<member_id>/subaccounts/open", methods=["POST"])
def subaccount_open(member_id: str):
    m = MEMBERS.get(member_id) or abort(404)
    product = request.form.get("product", "Savings")
    prefix = {"Savings": "S", "Money Market": "M", "Certificate": "T"}.get(product, "S")
    n = sum(1 for a in m["accounts"] if a["number"].split("-")[1].startswith(prefix)) + 1
    number = f"{member_id}-{prefix}{n:02d}"
    deposit = request.form.get("deposit", "0.00")
    m["accounts"].append({"number": number, "type": product, "status": "Open",
                          "balance": deposit, "available": deposit,
                          "nickname": request.form.get("nickname", "")})
    return redirect(url_for("subaccount_done", member_id=member_id, number=number))


@app.route("/main/members/<member_id>/subaccounts/<number>")
def subaccount_done(member_id: str, number: str):
    m = MEMBERS.get(member_id) or abort(404)
    acct = next((a for a in m["accounts"] if a["number"] == number), None) or abort(404)
    return render_template("subaccount_done.html", member_id=member_id, m=m, acct=acct)


@app.route("/main/reports")
def reports():
    return render_template("denied.html", what="Reports"), 403


@app.route("/main/notice/ack", methods=["POST"])
def notice_ack():
    FAULTS.pop("notice", None)
    return redirect(request.form.get("next") or url_for("home"))


@app.route("/main/surprise/later", methods=["POST"])
def surprise_later():
    FAULTS.pop("surprise", None)
    return redirect(request.form.get("next") or url_for("home"))


@app.route("/main/surprise/change")
def surprise_change():
    return render_template("password.html")


# ---- test hooks. A real vendor app would not have these. -----------------------

@app.route("/__fault", methods=["GET", "POST"])
def fault():
    if request.method == "POST":
        kind = request.form.get("kind", "")
        mode = request.form.get("mode", "once")
        if mode == "clear":
            if kind:
                FAULTS.pop(kind, None)
            else:
                FAULTS.clear()
        elif kind in FAULT_KINDS:
            FAULTS[kind] = "sticky" if mode == "sticky" else "once"
        else:
            abort(400)
    return jsonify(FAULTS)


@app.route("/__reset", methods=["POST"])
def reset():
    global MEMBERS
    MEMBERS = copy.deepcopy(SEED_MEMBERS)
    FAULTS.clear()
    return jsonify({"ok": True})


def main() -> None:
    port = int(os.environ.get("MERIDIAN_PORT", "5057"))
    app.run(host="127.0.0.1", port=port, threaded=True)


if __name__ == "__main__":
    main()
