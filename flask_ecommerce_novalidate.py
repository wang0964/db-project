from flask import Flask, request, render_template, redirect, url_for, flash, abort
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
import os
from functools import wraps
import re

load_dotenv()

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")

# ======== Mongo (no schema validation) ========
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
client = MongoClient(MONGODB_URI)
db = client["ecommerce"]

# ======== Uploads ========
UPLOAD_FOLDER = os.path.join(app.static_folder, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ======== Login ========
login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.init_app(app)
login_manager.login_message = "Please sign in to continue."
login_manager.login_message_category = "error"

class User(UserMixin):
    def __init__(self, doc):
        self.id = str(doc["_id"])
        self.email = doc.get("email", "")
        self.name = doc.get("name", "")
        self.isAdmin = doc.get("isAdmin", False)

@login_manager.user_loader
def load_user(user_id):
    doc = db.users.find_one({"_id": ObjectId(user_id)})
    return User(doc) if doc else None

def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not getattr(current_user, "isAdmin", False):
            abort(403)
        return f(*args, **kwargs)
    return wrapper

# ======== Helpers ========
def rebuild_categories_tree():
    """Rebuild categories_tree from adjacency list categories."""
    db.categories_tree.delete_many({})
    nodes = list(db.categories.find({}))
    by_parent = {}
    for n in nodes:
        by_parent.setdefault(n.get("parentId"), []).append(n)

    def build_children(parent_id):
        children = []
        for n in by_parent.get(parent_id, []):
            children.append({
                "_id": n["_id"],
                "name": n.get("name"),
                "subcategories": build_children(n["_id"])
            })
        return children

    for root in by_parent.get(None, []):
        db.categories_tree.insert_one({
            "_id": root["_id"],
            "name": root.get("name"),
            "subcategories": build_children(root["_id"]),
            "createdAt": datetime.utcnow()
        })
    return True

def _build_tree_push_update(path_ids, new_node):
    if not path_ids:
        return {"$push": {"subcategories": new_node}}, [], "subcategories"
    parts = []
    for i in range(len(path_ids)):
        parts.append("subcategories")
        parts.append(f"$[lvl{i}]")
    parts.append("subcategories")
    update_path = ".".join(parts)
    af = [{f"lvl{i}._id": path_ids[i]} for i in range(len(path_ids))]
    return {"$push": {update_path: new_node}}, af, update_path

# ======== Home ========
@app.route("/")
def index():
    products = list(db.products.find({"status": "active"}))
    return render_template("index.html", products=products)

# ======== Auth ========
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        name = request.form["name"].strip()
        if db.users.find_one({"email": email}):
            flash("Email already exists", "error")
            return redirect(url_for("register"))
        db.users.insert_one({
            "email": email, "name": name, "isAdmin": False,
            "createdAt": datetime.utcnow(), "addresses": []
        })
        flash("Registered successfully. Please sign in.", "success")
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        doc = db.users.find_one({"email": email})
        if not doc:
            flash("User does not exist", "error")
            return redirect(url_for("login"))
        login_user(User(doc))
        flash("Signed in", "success")
        return redirect(url_for("index"))
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Signed out", "info")
    return redirect(url_for("index"))

# ======== Cart & Order ========
@app.route("/cart")
@login_required
def cart():
    cart_doc = db.carts.find_one({"userId": ObjectId(current_user.id)}) or {"items": []}
    return render_template("cart.html", cart=cart_doc)

@app.route("/cart/add", methods=["POST"])
@login_required
def add_to_cart():
    product_id = ObjectId(request.form["productId"])
    sku = request.form.get("sku", "default")
    qty = int(request.form.get("qty", 1))

    p = db.products.find_one({"_id": product_id})
    if not p:
        flash("Product not found", "error")
        return redirect(url_for("index"))

    price = float(p.get("price", 0.0))
    for v in (p.get("variants") or []):
        if v.get("sku") == sku:
            price = float(v.get("price", price))
            break

    db.carts.update_one({"userId": ObjectId(current_user.id)}, {"$setOnInsert": {"items": []}}, upsert=True)
    res = db.carts.update_one(
        {"userId": ObjectId(current_user.id), "items.sku": {"$ne": sku}},
        {"$push": {"items": {"productId": product_id, "sku": sku, "qty": qty, "priceSnapshot": price}}}
    )
    if res.modified_count == 0:
        db.carts.update_one(
            {"userId": ObjectId(current_user.id), "items.sku": sku},
            {"$inc": {"items.$.qty": qty}, "$set": {"updatedAt": datetime.utcnow()}}
        )
    flash("Added to cart", "success")
    return redirect(url_for("cart"))

@app.route("/checkout", methods=["GET", "POST"])
@login_required
def checkout():
    if request.method == "POST":
        cart_doc = db.carts.find_one({"userId": ObjectId(current_user.id)})
        if not cart_doc or not cart_doc.get("items"):
            return render_template("checkout.html", error="Cart is empty", order=None)
        items = cart_doc["items"]
        total = sum(float(it.get("priceSnapshot", 0.0)) * int(it.get("qty", 0)) for it in items)
        order_id = db.orders.insert_one({
            "userId": ObjectId(current_user.id),
            "status": "created",
            "items": items,
            "amount": {"total": float(round(total, 2))},
            "createdAt": datetime.utcnow()
        }).inserted_id
        db.carts.update_one({"userId": ObjectId(current_user.id)}, {"$set": {"items": []}})
        return render_template("checkout.html", order=str(order_id), total=float(round(total, 2)))
    return render_template("checkout.html", order=None)


# ======== Product Detail ========
@app.route("/products/view/<product_id>")
def product_detail(product_id):
    # Accept either pure 24-hex or strings like "ObjectId('...')"
    pid_str = product_id
    m = re.search(r'[0-9a-fA-F]{24}', pid_str or '')
    if not m:
        flash("Invalid product ID", "error")
        return redirect(url_for("index"))
    try:
        pid = ObjectId(m.group(0))
    except Exception:
        flash("Invalid product ID", "error")
        return redirect(url_for("index"))
    p = db.products.find_one({"_id": pid})
    if not p:
        flash("Product not found", "error")
        return redirect(url_for("index"))
    # Load category names (if any)
    cat_names = []
    if p.get("categoryIds"):
        cats = list(db.categories.find({"_id": {"$in": p["categoryIds"]}}))
        # show path or name
        for c in cats:
            cat_names.append(c.get("path") or c.get("name"))
    return render_template("product_detail.html", p=p, categories=cat_names)

# ======== Admin: Products ========
@app.route("/admin/products")
@login_required
@admin_required
def admin_products():
    products = list(db.products.find())
    return render_template("admin_products.html", products=products)

@app.route("/admin/products/add", methods=["GET", "POST"])
@login_required
@admin_required
def admin_add_product():
    if request.method == "POST":
        title = request.form["title"].strip()
        price = float(request.form["price"])
        sku = request.form["sku"].strip()
        # multi-categories
        category_ids = []
        for cid in request.form.getlist("categoryIds"):
            try:
                category_ids.append(ObjectId(cid))
            except Exception:
                pass
        db.products.insert_one({
            "title": title,
            "price": float(price),
            "status": "active",
            "variants": [{"sku": sku, "attrs": {}, "price": float(price)}],
            "categoryIds": category_ids,
            "images": [],
            "createdAt": datetime.utcnow()
        })
        flash("Product created", "success")
        return redirect(url_for("admin_products"))
    categories = list(db.categories.find({}))
    return render_template("admin_add_product.html", categories=categories)

@app.route("/admin/products/edit/<product_id>", methods=["GET", "POST"])
@login_required
@admin_required
def admin_edit_product(product_id):
    product = db.products.find_one({"_id": ObjectId(product_id)})
    if not product:
        flash("Product not found", "error")
        return redirect(url_for("admin_products"))
    if request.method == "POST":
        title = request.form["title"].strip()
        price = float(request.form["price"])
        sku = request.form["sku"].strip()
        category_ids = []
        for cid in request.form.getlist("categoryIds"):
            try:
                category_ids.append(ObjectId(cid))
            except Exception:
                pass
        db.products.update_one(
            {"_id": ObjectId(product_id)},
            {"$set": {
                "title": title,
                "price": float(price),
                "variants": [{"sku": sku, "attrs": {}, "price": float(price)}],
                "categoryIds": category_ids
            }}
        )
        flash("Product updated", "success")
        return redirect(url_for("admin_products"))
    categories = list(db.categories.find({}))
    return render_template("admin_edit_product.html", product=product, categories=categories)

@app.route("/admin/products/delete/<product_id>")
@login_required
@admin_required
def admin_delete_product(product_id):
    db.products.delete_one({"_id": ObjectId(product_id)})
    # 可选：删除其上传目录
    folder = os.path.join(UPLOAD_FOLDER, product_id)
    if os.path.isdir(folder):
        try:
            for f in os.listdir(folder):
                os.remove(os.path.join(folder, f))
            os.rmdir(folder)
        except Exception:
            pass
    flash("Product deleted", "info")
    return redirect(url_for("admin_products"))

# ======== Product Images ========
@app.route("/admin/products/<product_id>/upload_images", methods=["POST"])
@login_required
@admin_required
def upload_product_images(product_id):
    try:
        pid = ObjectId(product_id)
    except Exception:
        flash("Invalid product ID", "error")
        return redirect(url_for("admin_products"))
    product = db.products.find_one({"_id": pid})
    if not product:
        flash("Product not found", "error")
        return redirect(url_for("admin_products"))

    files = request.files.getlist("images")
    saved_paths = []
    for f in files:
        if not f or not getattr(f, "filename", ""):
            continue
        filename = secure_filename(f.filename)
        name, ext = os.path.splitext(filename)
        unique = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{name}{ext}".lower()
        folder = os.path.join(UPLOAD_FOLDER, str(product_id))
        os.makedirs(folder, exist_ok=True)
        abs_path = os.path.join(folder, unique)
        f.save(abs_path)
        rel_path = os.path.join("uploads", str(product_id), unique).replace("\\", "/")
        saved_paths.append(rel_path)
    if saved_paths:
        db.products.update_one({"_id": pid}, {"$push": {"images": {"$each": saved_paths}}})
        flash(f"Uploaded {len(saved_paths)} images", "success")
    else:
        flash("No file selected", "error")
    return redirect(url_for("admin_edit_product", product_id=product_id))

@app.route("/admin/products/<product_id>/delete_image", methods=["POST"])
@login_required
@admin_required
def delete_product_image(product_id):
    img = request.form.get("img")
    try:
        pid = ObjectId(product_id)
    except Exception:
        flash("Invalid product ID", "error")
        return redirect(url_for("admin_products"))
    if not img:
        flash("Missing image parameter", "error")
        return redirect(url_for("admin_edit_product", product_id=product_id))
    safe_prefix = f"uploads/{product_id}/"
    if not img.startswith(safe_prefix):
        flash("Invalid image path", "error")
        return redirect(url_for("admin_edit_product", product_id=product_id))
    abs_path = os.path.join(app.static_folder, img).replace("\\", "/")
    if os.path.exists(abs_path):
        try:
            os.remove(abs_path)
        except Exception:
            pass
    db.products.update_one({"_id": pid}, {"$pull": {"images": img}})
    flash("Image deleted", "info")
    return redirect(url_for("admin_edit_product", product_id=product_id))

# ======== Admin: Categories (list/edit/delete) ========

@app.route("/admin/categories")
@login_required
@admin_required
def admin_categories():
    nodes = list(db.categories.find({}))
    id2name = {str(n["_id"]): n.get("name") for n in nodes}
    for n in nodes:
        if n.get("parentId"):
            n["parentName"] = id2name.get(str(n["parentId"]), "Unknown")
        else:
            n["parentName"] = "Root"
    return render_template("admin_categories.html", nodes=nodes)



@app.route("/admin/categories/add", methods=["GET", "POST"])
@login_required
@admin_required
def admin_category_add():
    if request.method == "POST":
        name = request.form["name"].strip()
        parent_id_str = request.form.get("parentId", "").strip()
        parent_id = ObjectId(parent_id_str) if parent_id_str else None

        if parent_id:
            parent = db.categories.find_one({"_id": parent_id})
            if not parent:
                flash("Parent category not found", "error")
                return redirect(url_for("admin_category_add"))
            path_ids = parent.get("pathIds", []) + [parent["_id"]]
            path = (parent.get("path") or parent["name"]) + ">" + name
        else:
            path_ids = []
            path = name

        new_id = db.categories.insert_one({
            "name": name,
            "parentId": parent_id,
            "pathIds": path_ids,
            "path": path,
            "createdAt": datetime.utcnow(),
        }).inserted_id

        if not parent_id:
            db.categories_tree.insert_one({
                "_id": new_id, "name": name, "subcategories": [], "createdAt": datetime.utcnow()
            })
        else:
            root_id = path_ids[0] if path_ids else parent_id
            if not db.categories_tree.find_one({"_id": root_id}):
                root_doc = db.categories.find_one({"_id": root_id})
                db.categories_tree.insert_one({"_id": root_doc["_id"], "name": root_doc["name"], "subcategories": [], "createdAt": datetime.utcnow()})
            parent_doc = db.categories.find_one({"_id": parent_id})
            parent_chain_under_root = (parent_doc.get("pathIds", [])[1:]) + [parent_id] if parent_doc.get("pathIds") else [parent_id]
            new_node = {"_id": new_id, "name": name, "subcategories": []}
            update_spec, array_filters, _ = _build_tree_push_update(parent_chain_under_root, new_node)
            db.categories_tree.update_one({"_id": root_id}, update_spec, array_filters=array_filters if array_filters else None)

        flash("Category created", "success")
        return redirect(url_for("admin_categories"))

    nodes = list(db.categories.find({}))
    return render_template("admin_category_add.html", nodes=nodes)

@app.route("/categories/edit/<cat_id>", methods=["GET", "POST"])
@login_required
@admin_required
def categories_edit(cat_id):
    cat = db.categories.find_one({"_id": ObjectId(cat_id)})
    if not cat:
        flash("Category not found", "error")
        return redirect(url_for("admin_categories"))
    if request.method == "POST":
        new_name = request.form["name"].strip()
        db.categories.update_one({"_id": cat["_id"]}, {"$set": {"name": new_name}})
        if cat.get("parentId"):
            parent = db.categories.find_one({"_id": cat["parentId"]})
            parent_path = parent.get("path") or parent["name"]
            new_prefix = parent_path + ">" + new_name
        else:
            new_prefix = new_name
        old_prefix = cat.get("path", cat["name"])
        db.categories.update_one({"_id": cat["_id"]}, {"$set": {"path": new_prefix}})
        pipeline = [
            {"$match": {"_id": cat["_id"]}},
            {"$graphLookup": {
                "from": "categories",
                "startWith": "$_id",
                "connectFromField": "_id",
                "connectToField": "parentId",
                "as": "desc"
            }},
            {"$project": {"desc": 1}}
        ]
        res = list(db.categories.aggregate(pipeline))
        if res:
            for d in res[0]["desc"]:
                d_old = d.get("path", d["name"])
                if d_old.startswith(old_prefix):
                    d_new = new_prefix + d_old[len(old_prefix):]
                    db.categories.update_one({"_id": d["_id"]}, {"$set": {"path": d_new}})
        rebuild_categories_tree()
        flash("Category updated", "success")
        return redirect(url_for("admin_categories"))
    return render_template("categories_edit.html", cat=cat)

@app.route("/categories/delete/<cat_id>")
@login_required
@admin_required
def categories_delete(cat_id):
    try:
        start_id = ObjectId(cat_id)
    except Exception:
        flash("类别ID不合法", "error")
        return redirect(url_for("admin_categories"))
    pipeline = [
        {"$match": {"_id": start_id}},
        {"$graphLookup": {
            "from": "categories",
            "startWith": "$_id",
            "connectFromField": "_id",
            "connectToField": "parentId",
            "as": "descendants"
        }},
        {"$project": {
            "allIds": {"$concatArrays": [["$_id"], {"$map": {"input": "$descendants", "as": "d", "in": "$$d._id"}}]}
        }}
    ]
    res = list(db.categories.aggregate(pipeline))
    if not res:
        flash("Category not found", "error")
        return redirect(url_for("admin_categories"))
    all_ids = res[0]["allIds"]
    db.categories.delete_many({"_id": {"$in": all_ids}})
    db.products.update_many({}, {"$pull": {"categoryIds": {"$in": all_ids}}})
    rebuild_categories_tree()
    flash("Category and all descendants deleted. References removed from products.", "info")
    return redirect(url_for("admin_categories"))

# ======== Categories: tree add + search ========

@app.route("/categories", methods=["GET"])
@login_required
def categories_home():
    # Fuzzy keyword for category name/path
    q = (request.args.get("q") or "").strip()
    # Selected category checkboxes
    sel_ids_raw = request.args.getlist("categoryIds")

    # All categories for rendering checkboxes
    nodes = list(db.categories.find({}))

    selected_ids = []
    for cid in sel_ids_raw:
        try:
            selected_ids.append(ObjectId(cid))
        except Exception:
            pass

    # Fuzzy match categories by name or path
    matched_ids = []
    if q:
        regex = {"$regex": q, "$options": "i"}
        matched = list(db.categories.find({"$or": [{"name": regex}, {"path": regex}]}))
        matched_ids = [m["_id"] for m in matched]

    # Union of user-checked categories and fuzzy-matched categories
    seed_ids = list({*selected_ids, *matched_ids})

    # Expand to include all descendants for each seed id
    all_ids = set()
    for sid in seed_ids:
        pipeline = [
            {"$match": {"_id": sid}},
            {"$graphLookup": {
                "from": "categories",
                "startWith": "$_id",
                "connectFromField": "_id",
                "connectToField": "parentId",
                "as": "descendants"
            }},
            {"$project": {
                "allIds": {"$concatArrays": [["$_id"], {"$map": {"input": "$descendants", "as": "d", "in": "$$d._id"}}]}
            }}
        ]
        res = list(db.categories.aggregate(pipeline))
        if res:
            for x in res[0]["allIds"]:
                all_ids.add(x)

    products = []
    if all_ids:
        products = list(db.products.find({"categoryIds": {"$in": list(all_ids)}}))

    # Keep which checkboxes are checked
    checked_set = set(str(x) for x in selected_ids)

    return render_template("categories.html",
                           nodes=nodes,
                           products=products,
                           count=len(products),
                           q=q,
                           checked_set=checked_set)

    trees = list(db.categories_tree.find({}))
    return render_template("categories.html", nodes=nodes, trees=trees)

@app.route("/categories/add", methods=["POST"])
@login_required
@admin_required
def categories_add():
    name = request.form["name"].strip()
    parent_id_str = request.form.get("parentId", "").strip()
    parent_id = ObjectId(parent_id_str) if parent_id_str else None

    if parent_id:
        parent = db.categories.find_one({"_id": parent_id})
        if not parent:
            flash("父Category not found", "error")
            return redirect(url_for("categories_home"))
        path_ids = parent.get("pathIds", []) + [parent["_id"]]
        path = (parent.get("path") or parent["name"]) + ">" + name
    else:
        path_ids = []
        path = name

    new_id = db.categories.insert_one({
        "name": name,
        "parentId": parent_id,
        "pathIds": path_ids,
        "path": path,
        "createdAt": datetime.utcnow(),
    }).inserted_id

    if not parent_id:
        db.categories_tree.insert_one({
            "_id": new_id, "name": name, "subcategories": [], "createdAt": datetime.utcnow()
        })
    else:
        root_id = path_ids[0] if path_ids else parent_id
        if not db.categories_tree.find_one({"_id": root_id}):
            root_doc = db.categories.find_one({"_id": root_id})
            db.categories_tree.insert_one({"_id": root_doc["_id"], "name": root_doc["name"], "subcategories": [], "createdAt": datetime.utcnow()})
        parent_doc = db.categories.find_one({"_id": parent_id})
        parent_chain_under_root = (parent_doc.get("pathIds", [])[1:]) + [parent_id] if parent_doc.get("pathIds") else [parent_id]
        new_node = {"_id": new_id, "name": name, "subcategories": []}
        update_spec, array_filters, _ = _build_tree_push_update(parent_chain_under_root, new_node)
        db.categories_tree.update_one({"_id": root_id}, update_spec, array_filters=array_filters if array_filters else None)

    flash("已添加子类别", "success")
    return redirect(url_for("categories_home"))

@app.route("/products/by-category/<cat_id>")
def products_by_category(cat_id):
    try:
        start_id = ObjectId(cat_id)
    except Exception:
        flash("类别ID不合法", "error")
        return redirect(url_for("index"))
    pipeline = [
        {"$match": {"_id": start_id}},
        {"$graphLookup": {
            "from": "categories",
            "startWith": "$_id",
            "connectFromField": "_id",
            "connectToField": "parentId",
            "as": "descendants"
        }},
        {"$project": {
            "allIds": {"$concatArrays": [["$_id"], {"$map": {"input": "$descendants", "as": "d", "in": "$$d._id"}}]}
        }}
    ]
    res = list(db.categories.aggregate(pipeline))
    if not res:
        flash("Category not found", "error")
        return redirect(url_for("index"))
    all_ids = res[0]["allIds"]
    products = list(db.products.find({"categoryIds": {"$in": all_ids}}))
    return render_template("products_by_category.html", products=products, count=len(products))

# ======== Seed ========
def seed_if_empty():
    if db.products.count_documents({}) == 0:
        db.products.insert_many([
            {"title": "Smartphone X", "price": 699.0, "status": "active",
             "variants": [{"sku": "X-BLK-128", "attrs": {"color": "Black", "size": "128G"}, "price": 699.0}],
             "images": [], "createdAt": datetime.utcnow()},
            {"title": "Laptop Pro", "price": 1299.0, "status": "active",
             "variants": [{"sku": "LP-16-512", "attrs": {"ram": "16G", "ssd": "512G"}, "price": 1299.0}],
             "images": [], "createdAt": datetime.utcnow()},
        ])
    if db.categories.count_documents({}) == 0:
        root_id = db.categories.insert_one({"name": "Electronics", "parentId": None, "pathIds": [], "path": "Electronics", "createdAt": datetime.utcnow()}).inserted_id
        phone_id = db.categories.insert_one({"name": "Phones", "parentId": root_id, "pathIds": [root_id], "path": "Electronics>Phones", "createdAt": datetime.utcnow()}).inserted_id
        food_id = db.categories.insert_one({"name": "Food", "parentId": None, "pathIds": [], "path": "Food", "createdAt": datetime.utcnow()}).inserted_id
        seafood_id = db.categories.insert_one({"name": "SeaFood", "parentId": food_id, "pathIds": [food_id], "path": "Food>SeaFood", "createdAt": datetime.utcnow()}).inserted_id
        fish_id = db.categories.insert_one({"name": "Fish", "parentId": seafood_id, "pathIds": [food_id, seafood_id], "path": "Food>SeaFood>Fish", "createdAt": datetime.utcnow()}).inserted_id
        rebuild_categories_tree()

# ======== Dev helper ========
@app.route("/dev/make_admin/<email>")
def dev_make_admin(email):
    db.users.update_one({"email": email.lower()}, {"$set": {"isAdmin": True}})
    return "OK"


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
        # save
        code_val = (request.form.get("code") or "").strip()
        if not code_val:
            flash("Invite code cannot be empty", "error")
            return redirect(url_for("admin_invite"))
        db.settings.update_one({"_id": "admin_invite_code"}, {"$set": {"value": code_val}}, upsert=True)
        flash("Invite code updated", "success")
        return redirect(url_for("admin_invite"))

    doc = db.settings.find_one({"_id": "admin_invite_code"}) or {}
    code_val = doc.get("value", "")
    return render_template("admin_invite.html", current_code=code_val)


if __name__ == "__main__":
    with app.app_context():
        seed_if_empty()
    app.run(debug=True)
