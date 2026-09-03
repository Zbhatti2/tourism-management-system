"""
Module — Inventory: Products. Tenant-scoped master inventory of physical
goods, each identified by a Product Code (Category-Group-Sub-Group-Product,
e.g. 'BP-CM-FC-0001') plus a Product Name — the second of Inventory
Management's two sub-modules (see blueprints/services.py for the first,
Services, which this module's shape deliberately mirrors).

Built from Claude_Code_Prompt_Products_Module.md and
TTMS_Products_Seed_Data.csv, per the request:

    "Build the Inventory Sub-Module 'Products' with Tables. Upload the Seed
    data."

The prompt was explicit that Products must follow Services' exact
conventions ("If Services already exists, mirror its exact schema shape
... even though it is a structurally separate registry") while being a
SEPARATE, INDEPENDENT registry -- product_categories/product_groups/
product_subgroups/products/product_attributes never join against or share
a uniqueness constraint with service_categories/service_groups/
service_subgroups/services. See schema.sql's MODULE J comment and
test_products.py's explicit proof of that independence.

The Category -> Group -> Sub-Group vocabulary itself
(product_categories/product_groups/product_subgroups) is GLOBAL, shared
reference data seeded by seed_data.seed_product_taxonomy() -- same
GLOBAL-taxonomy / tenant-scoped-catalog split as Services. This module
only manages the tenant-scoped `products` catalog built against that
shared vocabulary.

Deprecated Sub-Groups (validity_status='deprecated') are blocked from ever
being issued a code, three ways -- identical defense-in-depth to Services
(this taxonomy currently has zero deprecated combinations, per the
prompt's design notes, but the mechanism exists for future additions):

  1. UI layer -- _taxonomy_tree() below excludes 'deprecated' Sub-Groups
     entirely, so the New Product form's picker never even offers one.
  2. This route layer -- new_product() re-checks validity_status before
     the INSERT, in case the request was tampered with.
  3. Database layer -- schema.sql's trg_block_deprecated_product_subgroup
     trigger is the last line of defense, catching anything that bypassed
     both layers above (a bulk script, a future API).

A Product's Category/Group/Sub-Group and sequence number are immutable
once issued (edit_product() only lets Name/Description/SKU/Price/Cost/
Currency/Stock Quantity/Status change) -- same "a code is a stable
identifier once issued" rationale as Services. "Delete" discontinues
(status='discontinued') rather than removing the row -- Products' analog
of Services' status='archived' (a different word because a physical
product that's no longer carried is "discontinued," not "archived," but
the same never-destroy-just-stop-using philosophy) -- restore_product()
below reverses it back to 'draft' (see that route for why not 'active').

Deviations from the source prompt, flagged here per its own acceptance
criteria ("Summarize any place you deviated from this prompt"): no
separate product_code registry table (denormalized onto products.
product_code, matching how Services has no service_code table either);
products is tenant-scoped, not global; category-specific attributes use a
normalized product_attributes key-value table, not a JSON column. Full
reasoning in schema.sql's MODULE J comment and migrate_add_products.py's
docstring.
"""
import sqlite3

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from auth.decorators import login_required
from db import get_db, log_action

products_bp = Blueprint("products", __name__)


def _taxonomy_tree(db):
    """Categories -> Groups -> Sub-Groups, nested for the New Product
    form's cascading picker (see static/js/product_code_picker.js).
    Active categories/groups/subgroups only, and 'deprecated' Sub-Groups
    excluded outright (layer 1 of the module docstring's defense-in-depth)
    -- 'under_review' ones are included (the picker flags them, doesn't
    block them; only the database trigger and the layer-2 check below
    block 'deprecated')."""
    rows = db.execute(
        """SELECT c.category_id, c.category_code, c.category_name,
                  g.group_id, g.group_code, g.group_name,
                  sg.subgroup_id, sg.subgroup_code, sg.subgroup_name, sg.validity_status
           FROM product_categories c
           JOIN product_groups g ON g.category_id = c.category_id
           JOIN product_subgroups sg ON sg.group_id = g.group_id
           WHERE c.is_active = 1 AND g.is_active = 1 AND sg.is_active = 1
             AND sg.validity_status != 'deprecated'
           ORDER BY c.category_code, g.group_code, sg.subgroup_code"""
    ).fetchall()

    categories = {}
    order = []
    for r in rows:
        if r["category_id"] not in categories:
            categories[r["category_id"]] = {
                "category_id": r["category_id"], "category_code": r["category_code"],
                "category_name": r["category_name"], "groups": {}, "group_order": [],
            }
            order.append(r["category_id"])
        cat = categories[r["category_id"]]
        if r["group_id"] not in cat["groups"]:
            cat["groups"][r["group_id"]] = {
                "group_id": r["group_id"], "group_code": r["group_code"],
                "group_name": r["group_name"], "subgroups": [],
            }
            cat["group_order"].append(r["group_id"])
        cat["groups"][r["group_id"]]["subgroups"].append({
            "subgroup_id": r["subgroup_id"], "subgroup_code": r["subgroup_code"],
            "subgroup_name": r["subgroup_name"], "validity_status": r["validity_status"],
        })

    tree = []
    for cat_id in order:
        cat = categories[cat_id]
        cat["groups"] = [cat["groups"][gid] for gid in cat["group_order"]]
        del cat["group_order"]
        tree.append(cat)
    return tree


def _subgroup_full(db, subgroup_id):
    return db.execute(
        """SELECT sg.subgroup_id, sg.subgroup_code, sg.subgroup_name, sg.validity_status,
                  g.group_id, g.group_code, g.group_name,
                  c.category_id, c.category_code, c.category_name
           FROM product_subgroups sg
           JOIN product_groups g ON g.group_id = sg.group_id
           JOIN product_categories c ON c.category_id = g.category_id
           WHERE sg.subgroup_id = ?""",
        (subgroup_id,),
    ).fetchone()


def _product_row(db, product_id):
    return db.execute(
        """SELECT p.*, c.category_id, c.category_code, c.category_name,
                  g.group_code, g.group_name, sg.subgroup_code, sg.subgroup_name, sg.validity_status
           FROM products p
           JOIN product_subgroups sg ON sg.subgroup_id = p.subgroup_id
           JOIN product_groups g ON g.group_id = sg.group_id
           JOIN product_categories c ON c.category_id = g.category_id
           WHERE p.product_id = ? AND p.tenant_id = ?""",
        (product_id, g.tenant_id),
    ).fetchone()


@products_bp.route("/")
@login_required
def list_products():
    db = get_db()
    q = request.args.get("q", "").strip()
    category_id = request.args.get("category_id", "").strip()
    status = request.args.get("status", "").strip()
    show_discontinued = request.args.get("show_discontinued") == "1"

    sql = """
        SELECT p.*, c.category_id, c.category_code, c.category_name,
               g.group_code, g.group_name, sg.subgroup_code, sg.subgroup_name
        FROM products p
        JOIN product_subgroups sg ON sg.subgroup_id = p.subgroup_id
        JOIN product_groups g ON g.group_id = sg.group_id
        JOIN product_categories c ON c.category_id = g.category_id
        WHERE p.tenant_id = ?
    """
    params = [g.tenant_id]
    if not show_discontinued:
        sql += " AND p.status != 'discontinued'"
    if q:
        sql += " AND (p.product_name LIKE ? OR p.product_code LIKE ? OR p.sku LIKE ?)"
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if category_id:
        sql += " AND c.category_id = ?"
        params.append(category_id)
    if status:
        sql += " AND p.status = ?"
        params.append(status)
    sql += " ORDER BY p.product_code"
    rows = db.execute(sql, params).fetchall()

    categories = db.execute(
        "SELECT category_id, category_code, category_name FROM product_categories WHERE is_active = 1 ORDER BY category_code"
    ).fetchall()
    return render_template(
        "products/list.html", products=rows, q=q, category_id=category_id, status=status,
        show_discontinued=show_discontinued, categories=categories,
    )


@products_bp.route("/<int:product_id>")
@login_required
def view_product(product_id):
    db = get_db()
    product = _product_row(db, product_id)
    if product is None:
        abort(404)
    return render_template("products/view.html", product=product)


@products_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_product():
    db = get_db()
    if request.method == "POST":
        subgroup_id = request.form.get("subgroup_id") or None
        product_name = request.form.get("product_name", "").strip()
        description = request.form.get("description", "").strip() or None
        sku = request.form.get("sku", "").strip() or None
        price = request.form.get("price", "").strip() or None
        cost = request.form.get("cost", "").strip() or None
        currency = request.form.get("currency", "").strip() or None
        stock_quantity = request.form.get("stock_quantity", "").strip() or "0"
        status = request.form.get("status") or "draft"
        if status not in ("draft", "active", "discontinued"):
            status = "draft"

        subgroup = _subgroup_full(db, subgroup_id) if subgroup_id else None
        errors = []
        if subgroup is None:
            errors.append("Please choose a Category, Group, and Sub-Group.")
        elif subgroup["validity_status"] == "deprecated":
            # Layer 2 (see module docstring) — the picker already excludes
            # deprecated Sub-Groups, so reaching this means the request
            # was tampered with; the database trigger would catch it
            # anyway even without this check.
            errors.append(
                f"{subgroup['category_code']}-{subgroup['group_code']}-{subgroup['subgroup_code']} has been "
                "retired and can no longer be used for a new product."
            )
        if not product_name:
            errors.append("Product Name is required.")
        try:
            price_val = float(price) if price else None
        except ValueError:
            errors.append("Price must be a number.")
            price_val = None
        try:
            cost_val = float(cost) if cost else None
        except ValueError:
            errors.append("Cost must be a number.")
            cost_val = None
        try:
            stock_val = int(stock_quantity)
        except ValueError:
            errors.append("Stock Quantity must be a whole number.")
            stock_val = 0

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "products/form.html", product=None, taxonomy=_taxonomy_tree(db), form_values=request.form,
            )

        next_seq = db.execute(
            "SELECT COALESCE(MAX(sequence_number), 0) + 1 AS n FROM products WHERE tenant_id = ? AND subgroup_id = ?",
            (g.tenant_id, subgroup["subgroup_id"]),
        ).fetchone()["n"]
        product_code = f"{subgroup['category_code']}-{subgroup['group_code']}-{subgroup['subgroup_code']}-{next_seq:04d}"

        try:
            db.execute(
                """INSERT INTO products (tenant_id, subgroup_id, sequence_number, product_code, product_name,
                                          description, sku, price, cost, currency, stock_quantity, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (g.tenant_id, subgroup["subgroup_id"], next_seq, product_code, product_name,
                 description, sku, price_val, cost_val, currency, stock_val, status),
            )
            db.commit()
        except sqlite3.Error as e:
            # Layer 3 (see module docstring) — the database trigger.
            # Practically unreachable given the checks above, but a
            # deprecated-Sub-Group insert must never be silently swallowed.
            flash(f"Could not create the product: {e}", "error")
            return render_template(
                "products/form.html", product=None, taxonomy=_taxonomy_tree(db), form_values=request.form,
            )

        product_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        log_action("Create", "product", product_id, f"Created product {product_code}: {product_name}")
        flash(f"Product {product_code} created.", "success")
        return redirect(url_for("products.view_product", product_id=product_id))

    return render_template("products/form.html", product=None, taxonomy=_taxonomy_tree(db), form_values=None)


@products_bp.route("/<int:product_id>/edit", methods=["GET", "POST"])
@login_required
def edit_product(product_id):
    db = get_db()
    product = _product_row(db, product_id)
    if product is None:
        abort(404)

    if request.method == "POST":
        product_name = request.form.get("product_name", "").strip()
        description = request.form.get("description", "").strip() or None
        sku = request.form.get("sku", "").strip() or None
        price = request.form.get("price", "").strip() or None
        cost = request.form.get("cost", "").strip() or None
        currency = request.form.get("currency", "").strip() or None
        stock_quantity = request.form.get("stock_quantity", "").strip() or "0"
        status = request.form.get("status") or "draft"
        if status not in ("draft", "active", "discontinued"):
            status = "draft"

        errors = []
        if not product_name:
            errors.append("Product Name is required.")
        try:
            price_val = float(price) if price else None
        except ValueError:
            errors.append("Price must be a number.")
            price_val = None
        try:
            cost_val = float(cost) if cost else None
        except ValueError:
            errors.append("Cost must be a number.")
            cost_val = None
        try:
            stock_val = int(stock_quantity)
        except ValueError:
            errors.append("Stock Quantity must be a whole number.")
            stock_val = 0

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("products/form.html", product=product, taxonomy=None, form_values=request.form)

        db.execute(
            """UPDATE products SET product_name=?, description=?, sku=?, price=?, cost=?, currency=?,
                                    stock_quantity=?, status=?, updated_at=datetime('now')
               WHERE product_id=? AND tenant_id=?""",
            (product_name, description, sku, price_val, cost_val, currency, stock_val, status,
             product_id, g.tenant_id),
        )
        db.commit()
        log_action("Update", "product", product_id, f"Updated product {product['product_code']}")
        flash("Product updated.", "success")
        return redirect(url_for("products.view_product", product_id=product_id))

    return render_template("products/form.html", product=product, taxonomy=None, form_values=None)


@products_bp.route("/<int:product_id>/delete", methods=["POST"])
@login_required
def delete_product(product_id):
    db = get_db()
    product = db.execute(
        "SELECT * FROM products WHERE product_id = ? AND tenant_id = ?", (product_id, g.tenant_id)
    ).fetchone()
    if product is None:
        abort(404)
    db.execute(
        "UPDATE products SET status = 'discontinued', updated_at = datetime('now') WHERE product_id = ? AND tenant_id = ?",
        (product_id, g.tenant_id),
    )
    db.commit()
    log_action("Delete", "product", product_id, f"Discontinued product {product['product_code']}")
    flash(f"'{product['product_code']}' discontinued.", "success")
    return redirect(url_for("products.list_products"))


@products_bp.route("/<int:product_id>/restore", methods=["POST"])
@login_required
def restore_product(product_id):
    db = get_db()
    product = db.execute(
        "SELECT * FROM products WHERE product_id = ? AND tenant_id = ?", (product_id, g.tenant_id)
    ).fetchone()
    if product is None:
        abort(404)
    # Restores to 'draft', not 'active' -- a discontinued product coming
    # back into the catalog needs a fresh look (price/cost/stock review)
    # before it's sellable again, the same reasoning the seed data itself
    # uses for every newly-created row (see seed_data.seed_product_catalog).
    db.execute(
        "UPDATE products SET status = 'draft', updated_at = datetime('now') WHERE product_id = ? AND tenant_id = ?",
        (product_id, g.tenant_id),
    )
    db.commit()
    log_action("Update", "product", product_id, f"Restored product {product['product_code']} from discontinued")
    flash(f"'{product['product_code']}' restored to Draft.", "success")
    return redirect(request.referrer or url_for("products.view_product", product_id=product_id))
