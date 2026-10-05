"""
Tour Design: each user's To-Do List and Prompt List (tour_tools.py).

* tour_todos_bp (/tour-planner/todos): the JSON behind the To-Do modal
  (templates/tour_planner/_todo_modal.html, static/js/tour_todos.js),
  opened from the To-Do button beside Help on the Tour Design pages.
* tour_prompts_bp (/tour-planner/prompts): the Prompt List -- list with
  Group, Title and Date last used; open, change, save, save as new,
  delete, copy, and "Use for a new Tour Design".
"""
from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

import package_groups
import tour_tools
from auth.decorators import login_required
from db import get_db, log_action

tour_todos_bp = Blueprint("tour_todos", __name__)
tour_prompts_bp = Blueprint("tour_prompts", __name__)


def _tenant():
    if not g.get("tenant_id"):
        abort(403)


# ---- To-Do List ---------------------------------------------------------------------------------

@tour_todos_bp.route("/")
@login_required
def list_todos():
    _tenant()
    db = get_db()
    rows = tour_tools.todos(db, g.tenant_id, g.user_id, subject=(request.args.get("subject") or "").strip() or None,
                            status=request.args.get("status") or None, sort=request.args.get("sort", "created_at"),
                            direction=request.args.get("dir", "desc"),
                            plan_id=request.args.get("plan", type=int) if request.args.get("only_plan") else None)
    for r in rows:
        r["plan_url"] = url_for("tour_planner.workspace", plan_id=r["plan_id"]) if r["plan_id"] else None
    return jsonify({"todos": rows, "open": tour_tools.open_todo_count(db, g.tenant_id, g.user_id)})


@tour_todos_bp.route("/", methods=["POST"])
@login_required
def add_todo():
    _tenant()
    try:
        tid = tour_tools.add_todo(get_db(), g.tenant_id, g.user_id, request.form.get("subject"), request.form.get("task"),
                                  request.form.get("plan_id", type=int))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"todo_id": tid}), 201


@tour_todos_bp.route("/<int:todo_id>/status", methods=["POST"])
@login_required
def todo_status(todo_id):
    _tenant()
    try:
        tour_tools.set_todo_status(get_db(), g.tenant_id, g.user_id, todo_id, request.form.get("status"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@tour_todos_bp.route("/<int:todo_id>/delete", methods=["POST"])
@login_required
def todo_delete(todo_id):
    _tenant()
    tour_tools.delete_todo(get_db(), g.tenant_id, g.user_id, todo_id)
    return jsonify({"ok": True})


# ---- Prompt List --------------------------------------------------------------------------------

def _group_id(db, raw):
    return package_groups.valid_id(db, g.tenant_id, raw)


@tour_prompts_bp.route("/")
@login_required
def index():
    _tenant()
    db = get_db()
    tour_tools.seed_prompts(db, g.tenant_id, g.user_id)
    group = (request.args.get("group") or "").strip()
    q = (request.args.get("q") or "").strip()
    sort = request.args.get("sort", "last_used")
    rows = tour_tools.prompts(db, g.tenant_id, g.user_id, group=group, q=q, sort=sort)
    return render_template("tour_planner/prompts.html", prompts=rows, group=group, q=q, sort=sort,
                           package_groups=package_groups.choices(db, g.tenant_id), current=None,
                           help_id="tour_planner/todo-and-prompts")


@tour_prompts_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_prompt():
    _tenant()
    db = get_db()
    if request.method == "POST":
        try:
            pid = tour_tools.create_prompt(db, g.tenant_id, g.user_id, request.form.get("title"), request.form.get("body"),
                                           _group_id(db, request.form.get("package_group_id")))
        except ValueError as e:
            flash(str(e), "error")
            return render_template("tour_planner/prompt_form.html", help_id="tour_planner/todo-and-prompts", p=None, form=request.form,
                                   package_groups=package_groups.choices(db, g.tenant_id))
        log_action("Create", "agent_prompts", pid, "Saved a new prompt")
        flash("Prompt saved.", "success")
        return redirect(url_for("tour_prompts.open_prompt", prompt_id=pid))
    return render_template("tour_planner/prompt_form.html", help_id="tour_planner/todo-and-prompts", p=None, form=None,
                           package_groups=package_groups.choices(db, g.tenant_id))


@tour_prompts_bp.route("/<int:prompt_id>", methods=["GET", "POST"])
@login_required
def open_prompt(prompt_id):
    """Open a prompt: read it, change it, Save (this prompt) or Save as new."""
    _tenant()
    db = get_db()
    p = tour_tools.prompt(db, g.tenant_id, g.user_id, prompt_id)
    if p is None:
        abort(404)
    if request.method == "POST":
        f = request.form
        gid = _group_id(db, f.get("package_group_id"))
        try:
            if f.get("action") == "save_as_new":
                title = (f.get("title") or "").strip()
                if title == p["title"]:
                    title = tour_tools.untitled_copy_title(db, g.tenant_id, g.user_id, title)
                new_id = tour_tools.create_prompt(db, g.tenant_id, g.user_id, title, f.get("body"), gid, based_on=prompt_id,
                                                  agent=p["agent"])
                log_action("Create", "agent_prompts", new_id, f"Saved prompt as new: {title}")
                flash(f"Saved as a new prompt: “{title}”. The original is unchanged.", "success")
                return redirect(url_for("tour_prompts.open_prompt", prompt_id=new_id))
            tour_tools.update_prompt(db, g.tenant_id, g.user_id, prompt_id, f.get("title"), f.get("body"), gid)
        except ValueError as e:
            flash(str(e), "error")
            return render_template("tour_planner/prompt_form.html", help_id="tour_planner/todo-and-prompts", p=p, form=f,
                                   package_groups=package_groups.choices(db, g.tenant_id, gid))
        log_action("Update", "agent_prompts", prompt_id, "Updated a prompt")
        flash("Prompt saved.", "success")
        return redirect(url_for("tour_prompts.open_prompt", prompt_id=prompt_id))
    return render_template("tour_planner/prompt_form.html", help_id="tour_planner/todo-and-prompts", p=p, form=None,
                           package_groups=package_groups.choices(db, g.tenant_id, p["package_group_id"]))


@tour_prompts_bp.route("/<int:prompt_id>/delete", methods=["POST"])
@login_required
def delete_prompt(prompt_id):
    _tenant()
    db = get_db()
    p = tour_tools.prompt(db, g.tenant_id, g.user_id, prompt_id)
    if p is None:
        abort(404)
    tour_tools.delete_prompt(db, g.tenant_id, g.user_id, prompt_id)
    log_action("Delete", "agent_prompts", prompt_id, f"Deleted prompt {p['title']}")
    flash(f"Deleted “{p['title']}”.", "success")
    return redirect(url_for("tour_prompts.index"))


@tour_prompts_bp.route("/<int:prompt_id>/used", methods=["POST"])
@login_required
def used(prompt_id):
    """Copied to the clipboard: counts as used (Date last used)."""
    _tenant()
    tour_tools.mark_used(get_db(), g.tenant_id, g.user_id, prompt_id)
    return jsonify({"ok": True})


@tour_prompts_bp.route("/<int:prompt_id>/use")
@login_required
def use(prompt_id):
    """Point the agent at the prompt: a new Tour Design with it as the request."""
    _tenant()
    if tour_tools.prompt(get_db(), g.tenant_id, g.user_id, prompt_id) is None:
        abort(404)
    return redirect(url_for("tour_planner.new_plan", prompt=prompt_id))
