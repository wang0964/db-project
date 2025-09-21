
import os
import re
from datetime import datetime
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, flash, Response
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from bson import ObjectId
from pymongo import MongoClient
from gridfs import GridFS
from dotenv import load_dotenv

load_dotenv()

MONGO_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "ecommerce")

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")

client = MongoClient(MONGO_URI)
db = client[DB_NAME]
fs = GridFS(db)

# --- create important index (idempotent) ---
try:
    db.carts.create_index([("userId", 1), ("product_id", 1)], unique=True)
except Exception:
    pass

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "Please sign in to continue."
login_manager.login_message_category = "error"


class User(UserMixin):
    def __init__(self, doc):
        self.id = str(doc["_id"])
        self.email = doc["email"]
        self.name = doc.get("name") or self.email.split("@")[0]
        self.isAdmin = bool(doc.get("isAdmin"))


@login_manager.user_loader
def load_user(user_id):
    u = db.users.find_one({"_id": ObjectId(user_id)})
    return User(u) if u else None


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if not getattr(current_user, "isAdmin", False):
            flash("Admins only.", "error")
            return redirect(url_for("index"))
        return f(*args, **kwargs)

    return wrapper


# --------------- helpers ---------------
def oid(s):
    m = re.search(r"[0-9a-fA-F]{24}", s or "")
    if not m:
        return None
    try:
        return ObjectId(m.group(0))
    except Exception:
        return None


def _get_pid(cart_doc):
    """Read product ObjectId from a cart item; support product_id & productId/pid (legacy)."""
    val = cart_doc.get("product_id") or cart_doc.get("productId") or cart_doc.get("pid")
    if isinstance(val, ObjectId):
        return val
    if not val:
        return None
    m = re.search(r"[0-9a-fA-F]{24}", str(val))
    return ObjectId(m.group(0)) if m else None


def _normalize_cart(user_oid):
    """Normalize current user's cart: unifies product_id field, merges duplicates, drops invalid items."""
    items = list(db.carts.find({"userId": user_oid}))
    for it in items:
        pid = _get_pid(it)
        if not pid:
            db.carts.delete_one({"_id": it["_id"]})
            continue
        if it.get("product_id") != pid or any(k in it for k in ("productId", "pid")):
            db.carts.update_one(
                {"_id": it["_id"]},
                {"$set": {"product_id": pid}, "$unset": {"productId": "", "pid": ""}},
            )
    # merge duplicates
    groups = db.carts.aggregate(
        [{"$match": {"userId": user_oid}}, {"$group": {"_id": "$product_id", "ids": {"$push": "$_id"}, "sumQty": {"$sum": "$qty"}}}]
    )
    for g in groups:
        ids = g["ids"]
        if len(ids) > 1:
            keep = ids[0]
            db.carts.update_one({"_id": keep}, {"$set": {"qty": g["sumQty"]}})
            db.carts.delete_many({"_id": {"$in": ids[1:]}})


def build_path(parent, name):
    return f"{parent['path']}>{name}" if parent else name


def get_all_descendant_ids(cat_id):
    node = db.categories.find_one({"_id": cat_id})
    if not node:
        return {cat_id}
    prefix = node["path"] + ">"
    ids = {node["_id"]}
    for c in db.categories.find({"path": {"$regex": f"^{re.escape(prefix)}"}}):
        ids.add(c["_id"])
    return ids


def first_image_url(p):
    ids = p.get("imageIds") or []
    if ids:
        return url_for("image_file", file_id=str(ids[0]))
    paths = p.get("images") or []
    if paths:
        return paths[0]
    return ""


# --------------- auth ---------------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        name = request.form["name"].strip()
        invite = (request.form.get("invite") or "").strip()

        if db.users.find_one({"email": email}):
            flash("Email already exists", "error")
            return redirect(url_for("register"))

        if db.users.count_documents({}) == 0:
            is_admin = True
        else:
            doc = db.settings.find_one({"_id": "admin_invite_code"}) or {}
            db_code = doc.get("value", "")
            admin_code = db_code or os.getenv("ADMIN_INVITE_CODE", "")
            is_admin = bool(admin_code and invite and invite == admin_code)

        db.users.insert_one(
            {
                "email": email,
                "name": name,
                "isAdmin": is_admin,
                "createdAt": datetime.utcnow(),
                "addresses": [],
            }
        )
        flash("Registered successfully. Please sign in.", "success")
        return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        u = db.users.find_one({"email": email})
        if not u:
            flash("User does not exist", "error")
            return redirect(url_for("login"))
        login_user(User(u))
        next_url = request.args.get("next")
        return redirect(next_url or url_for("index"))
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Signed out", "success")
    return redirect(url_for("index"))


# --------------- image serving (GridFS) ---------------
@app.route("/image/<file_id>")
def image_file(file_id):
    fid = oid(file_id)
    if not fid:
        return Response(status=404)
    try:
        gridout = fs.get(fid)
    except Exception:
        return Response(status=404)
    data = gridout.read()
    ctype = getattr(gridout, "content_type", None) or "application/octet-stream"
    return Response(data, mimetype=ctype)


# --------------- home & product detail ---------------
@app.route("/")
def index():
    products = list(db.products.find({"status": "active"}).sort("createdAt", -1))
    for p in products:
        p["_id"] = str(p["_id"])
        p["img0"] = first_image_url(p)
    return render_template("index.html", products=products)


@app.route("/products/view/<product_id>")
def product_detail(product_id):
    pid = oid(product_id)
    if not pid:
        flash("Invalid product ID", "error")
        return redirect(url_for("index"))
    p = db.products.find_one({"_id": pid})
    if not p:
        flash("Product not found", "error")
        return redirect(url_for("index"))

    img_ids = [str(x) for x in (p.get("imageIds") or [])]
    imgs = [url_for("image_file", file_id=i) for i in img_ids]
    if (not imgs) and p.get("images"):
        imgs = p["images"]

    cat_names = []
    ids = p.get("categoryIds", [])
    if ids:
        for c in db.categories.find({"_id": {"$in": ids}}):
            cat_names.append(c["path"])

    p["_id"] = str(p["_id"])
    return render_template("product_detail.html", p=p, categories=cat_names, imgs=imgs)


# --------------- cart & checkout ---------------
@app.route("/cart")
@login_required
def cart():
    _normalize_cart(ObjectId(current_user.id))
    items = list(db.carts.find({"userId": ObjectId(current_user.id)}))
    valid, total = [], 0.0
    for it in items:
        pid = _get_pid(it)
        if not pid:
            continue
        prod = db.products.find_one({"_id": pid})
        if not prod:
            continue
        it["product"] = prod
        total += (prod.get("price", 0) or 0) * it.get("qty", 0)
        valid.append(it)
    return render_template("cart.html", items=valid, total=total)


@app.route("/cart/add", methods=["POST"])
@login_required
def add_to_cart():
    pid = oid(request.form.get("product_id"))
    qty = int(request.form.get("qty", 1) or 1)
    p = db.products.find_one({"_id": pid, "status": "active"})
    if not p or int(p.get("stock", 0)) <= 0:
        flash("Product is unavailable or out of stock", "error")
        return redirect(url_for("product_detail", product_id=request.form.get("product_id")))
    available = int(p.get("stock", 0))
    if qty > available:
        flash(f"Only {available} left in stock", "error")
        return redirect(url_for("product_detail", product_id=request.form.get("product_id")))

    existing = db.carts.find_one({"userId": ObjectId(current_user.id), "product_id": pid})
    if existing:
        new_qty = existing["qty"] + qty
        if new_qty > available:
            flash(f"Only {available} left in stock", "error")
            return redirect(url_for("product_detail", product_id=request.form.get("product_id")))
        db.carts.update_one({"_id": existing["_id"]}, {"$set": {"qty": new_qty}})
    else:
        db.carts.insert_one({"userId": ObjectId(current_user.id), "product_id": pid, "qty": qty})

    flash("Added to cart", "success")
    return redirect(url_for("cart"))



@app.route("/cart/item/remove", methods=["POST"])
@login_required
def cart_item_remove():
    _normalize_cart(ObjectId(current_user.id))
    pid = oid(request.form.get("product_id"))
    if not pid:
        flash("Invalid item.", "error")
        return redirect(request.referrer or url_for("cart"))
    db.carts.delete_one({"userId": ObjectId(current_user.id), "product_id": pid})
    flash("Item removed", "success")
    return redirect(request.referrer or url_for("cart"))

@app.route("/checkout", methods=["GET", "POST"])
@login_required
def checkout():
    _normalize_cart(ObjectId(current_user.id))
    items = list(db.carts.find({"userId": ObjectId(current_user.id)}))
    if not items:
        flash("Cart is empty", "info")
        return redirect(url_for("cart"))

    for it in items:
        pid = _get_pid(it)
        if not pid:
            flash("Cart contains invalid item.", "error")
            return redirect(url_for("cart"))
        p = db.products.find_one({"_id": pid, "status": "active"})
        if (not p) or int(p.get("stock", 0)) < it.get("qty", 0):
            flash("Some items are out of stock. Please update your cart.", "error")
            return redirect(url_for("cart"))

    if request.method == "POST":
        total, lines = 0.0, []
        for it in items:
            pid = _get_pid(it)
            prod = db.products.find_one({"_id": pid})
            total += prod["price"] * it["qty"]
            lines.append(
                {"product_id": prod["_id"], "title": prod["title"], "price": prod["price"], "qty": it["qty"]}
            )

        order_id = db.orders.insert_one(
            {
                "userId": ObjectId(current_user.id),
                "lines": lines,
                "total": total,
                "status": "paid",
                "createdAt": datetime.utcnow(),
            }
        ).inserted_id

        for it in items:
            pid = _get_pid(it)
            db.products.update_one({"_id": pid}, {"$inc": {"stock": -it["qty"]}})

        db.carts.delete_many({"userId": ObjectId(current_user.id)})
        return render_template("checkout.html", order_id=str(order_id), total=total)

    total = 0.0
    for it in items:
        pid = _get_pid(it)
        prod = db.products.find_one({"_id": pid})
        if not prod:
            continue
        it["product"] = prod
        total += prod["price"] * it["qty"]
    return render_template("checkout.html", items=items, total=total)


# --------------- categories ---------------
@app.route("/categories", methods=["GET"])
@login_required
def categories_home():
    keyword = (request.args.get("q") or "").strip()
    selected = request.args.getlist("cat")
    cat_docs = list(db.categories.find({}).sort("path", 1))

    products = []
    if selected or keyword:
        selected_ids = set()
        for sid in selected:
            cid = oid(sid)
            if cid:
                selected_ids |= get_all_descendant_ids(cid)
        query = {"status": "active"}
        if selected_ids:
            query["categoryIds"] = {"$in": list(selected_ids)}
        if keyword:
            query["$or"] = [
                {"title": {"$regex": keyword, "$options": "i"}},
                {"sku": {"$regex": keyword, "$options": "i"}},
            ]
        products = list(db.products.find(query))
        for p in products:
            p["img0"] = first_image_url(p)

    return render_template("categories.html", categories=cat_docs, products=products, q=keyword, selected=selected)


# --------------- admin: products ---------------
@app.route("/admin/products")
@login_required
@admin_required
def admin_products():
    products = list(db.products.find({}).sort("createdAt", -1))
    return render_template("admin_products.html", products=products)


@app.route("/admin/products/add", methods=["GET", "POST"])
@login_required
@admin_required
def admin_add_product():
    cats = list(db.categories.find({}).sort("path", 1))
    if request.method == "POST":
        title = request.form["title"].strip()
        price = float(request.form["price"])
        sku = request.form["sku"].strip()
        status = "active" if request.form.get("is_active") else "inactive"
        stock = int(request.form.get("stock", 0) or 0)
        selected = [oid(x) for x in request.form.getlist("categories")]
        selected = [x for x in selected if x]
        new_id = db.products.insert_one(
            {
                "title": title,
                "price": price,
                "variants": [{"sku": sku}],
                "categoryIds": selected,
                "imageIds": [],
                "images": [],  # legacy
                "createdAt": datetime.utcnow(),
                "status": status,
                "stock": stock,
            }
        ).inserted_id
        flash("Product created", "success")
        return redirect(url_for("admin_edit_product", product_id=str(new_id)))
    return render_template("admin_add_product.html", categories=cats)


@app.route("/admin/products/edit/<product_id>", methods=["GET", "POST"])
@login_required
@admin_required
def admin_edit_product(product_id):
    pid = oid(product_id)
    p = db.products.find_one({"_id": pid})
    if not p:
        flash("Product not found", "error")
        return redirect(url_for("admin_products"))
    cats = list(db.categories.find({}).sort("path", 1))
    if request.method == "POST":
        title = request.form["title"].strip()
        price = float(request.form["price"])
        sku = request.form["sku"].strip()
        status = "active" if request.form.get("is_active") else "inactive"
        stock = int(request.form.get("stock", 0) or 0)
        selected = [oid(x) for x in request.form.getlist("categories")]
        selected = [x for x in selected if x]
        db.products.update_one(
            {"_id": pid},
            {
                "$set": {
                    "title": title,
                    "price": price,
                    "variants": [{"sku": sku}],
                    "categoryIds": selected,
                    "status": status,
                    "stock": stock,
                }
            },
        )
        flash("Product updated", "success")
        return redirect(url_for("admin_edit_product", product_id=product_id))

    img_ids = [str(x) for x in (p.get("imageIds") or [])]
    img_pairs = [{"id": i, "url": url_for("image_file", file_id=i)} for i in img_ids]
    return render_template("admin_edit_product.html", p=p, categories=cats, img_pairs=img_pairs)


@app.route("/admin/products/delete/<product_id>")
@login_required
@admin_required
def admin_delete_product(product_id):
    pid = oid(product_id)
    prod = db.products.find_one({"_id": pid}) or {}
    for fid in prod.get("imageIds", []):
        try:
            fs.delete(fid)
        except Exception:
            pass
    db.products.delete_one({"_id": pid})
    flash("Product deleted", "success")
    return redirect(url_for("admin_products"))


@app.route("/admin/products/<product_id>/images", methods=["POST"])
@login_required
@admin_required
def upload_images(product_id):
    pid = oid(product_id)
    files = request.files.getlist("images")
    ids = []
    for f in files:
        if not f.filename:
            continue
        data = f.read()
        fid = fs.put(data, filename=f.filename, content_type=f.mimetype, productId=pid)
        ids.append(fid)
    if ids:
        db.products.update_one({"_id": pid}, {"$push": {"imageIds": {"$each": ids}}})
        flash(f"Uploaded {len(ids)} images", "success")
    else:
        flash("No file selected", "info")
    return redirect(url_for("admin_edit_product", product_id=product_id))


@app.route("/admin/products/<product_id>/images/delete", methods=["POST"])
@login_required
@admin_required
def delete_image(product_id):
    pid = oid(product_id)
    fid = oid(request.form.get("img"))
    if not fid:
        flash("Invalid image id", "error")
        return redirect(url_for("admin_edit_product", product_id=product_id))
    try:
        fs.delete(fid)
    except Exception:
        pass
    db.products.update_one({"_id": pid}, {"$pull": {"imageIds": fid}})
    flash("Image deleted", "success")
    return redirect(url_for("admin_edit_product", product_id=product_id))


# --------------- admin: categories ---------------
@app.route("/admin/categories")
@login_required
@admin_required
def admin_categories():
    nodes = list(db.categories.find({}).sort("path", 1))
    id2name = {str(n["_id"]): n.get("name") for n in nodes}
    for n in nodes:
        n["parentName"] = id2name.get(str(n.get("parentId")), "Root") if n.get("parentId") else "Root"
    return render_template("admin_categories.html", nodes=nodes)


@app.route("/admin/categories/add", methods=["GET", "POST"])
@login_required
@admin_required
def admin_category_add():
    if request.method == "POST":
        name = request.form["name"].strip()
        parent_id_str = request.form.get("parentId", "").strip()
        parent = db.categories.find_one({"_id": ObjectId(parent_id_str)}) if parent_id_str else None
        path = build_path(parent, name)
        db.categories.insert_one(
            {"name": name, "parentId": parent["_id"] if parent else None, "path": path, "createdAt": datetime.utcnow()}
        )
        flash("Category created", "success")
        return redirect(url_for("admin_categories"))
    nodes = list(db.categories.find({}).sort("path", 1))
    return render_template("admin_category_add.html", nodes=nodes)


@app.route("/admin/categories/delete/<cat_id>")
@login_required
@admin_required
def admin_category_delete(cat_id):
    cid = oid(cat_id)
    c = db.categories.find_one({"_id": cid})
    if not c:
        flash("Category not found", "error")
        return redirect(url_for("admin_categories"))
    prefix = c["path"] + ">"
    to_delete = [c["_id"]] + [d["_id"] for d in db.categories.find({"path": {"$regex": f"^{re.escape(prefix)}"}})]
    db.categories.delete_many({"_id": {"$in": to_delete}})
    db.products.update_many({}, {"$pull": {"categoryIds": {"$in": to_delete}}})
    flash("Category and descendants deleted. Product refs updated.", "success")
    return redirect(url_for("admin_categories"))


# --------------- admin: invite code ---------------
@app.route("/admin/invite", methods=["GET", "POST"])
@login_required
@admin_required
def admin_invite():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "clear":
            db.settings.delete_one({"_id": "admin_invite_code"})
            flash("Invite code cleared", "info")
            return redirect(url_for("admin_invite"))
        code_val = (request.form.get("code") or "").strip()
        if not code_val:
            flash("Invite code cannot be empty", "error")
            return redirect(url_for("admin_invite"))
        db.settings.update_one({"_id": "admin_invite_code"}, {"$set": {"value": code_val}}, upsert=True)
        flash("Invite code updated", "success")
        return redirect(url_for("admin_invite"))
    doc = db.settings.find_one({"_id": "admin_invite_code"}) or {}
    return render_template("admin_invite.html", current_code=doc.get("value", ""))


if __name__ == "__main__":
    app.run(debug=True)
