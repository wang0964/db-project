from flask import Flask, request, render_template, redirect, url_for, flash, abort
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime
from dotenv import load_dotenv
import os
from functools import wraps

load_dotenv()

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")  # ⚠️ 生产请使用强随机值

# ======== Mongo（无 Schema 校验）========
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
client = MongoClient(MONGODB_URI)
db = client["ecommerce"]

# ======== Flask-Login ========
login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.init_app(app)

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

# ======== 首页 ========
@app.route("/")
def index():
    products = list(db.products.find({"status": "active"}))
    return render_template("index.html", products=products)

# ======== 注册 / 登录 / 登出 ========
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        name = request.form["name"].strip()
        if db.users.find_one({"email": email}):
            flash("邮箱已存在", "error")
            return redirect(url_for("register"))
        db.users.insert_one({
            "email": email, "name": name, "isAdmin": False,
            "createdAt": datetime.utcnow(), "addresses": []
        })
        flash("注册成功，请登录", "success")
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        doc = db.users.find_one({"email": email})
        if not doc:
            flash("用户不存在", "error")
            return redirect(url_for("login"))
        login_user(User(doc))
        flash("登录成功", "success")
        return redirect(url_for("index"))
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("已退出登录", "info")
    return redirect(url_for("index"))

# ======== 购物车 / 下单 ========
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
        flash("商品不存在", "error")
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
    flash("已加入购物车", "success")
    return redirect(url_for("cart"))

@app.route("/checkout", methods=["GET", "POST"])
@login_required
def checkout():
    if request.method == "POST":
        cart_doc = db.carts.find_one({"userId": ObjectId(current_user.id)})
        if not cart_doc or not cart_doc.get("items"):
            return render_template("checkout.html", error="购物车为空", order=None)
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

# ======== 后台：商品 CRUD ========
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
        cat_id = request.form.get("categoryId")
        category_id = ObjectId(cat_id) if cat_id else None
        db.products.insert_one({
            "title": title,
            "price": float(price),
            "status": "active",
            "variants": [{"sku": sku, "attrs": {}, "price": float(price)}],
            "categoryId": category_id,
            "createdAt": datetime.utcnow()
        })
        flash("商品已添加", "success")
        return redirect(url_for("admin_products"))
    categories = list(db.categories.find({}))
    return render_template("admin_add_product.html", categories=categories)

@app.route("/admin/products/edit/<product_id>", methods=["GET", "POST"])
@login_required
@admin_required
def admin_edit_product(product_id):
    product = db.products.find_one({"_id": ObjectId(product_id)})
    if not product:
        flash("商品不存在", "error")
        return redirect(url_for("admin_products"))
    if request.method == "POST":
        title = request.form["title"].strip()
        price = float(request.form["price"])
        sku = request.form["sku"].strip()
        cat_id = request.form.get("categoryId")
        category_id = ObjectId(cat_id) if cat_id else None
        db.products.update_one(
            {"_id": ObjectId(product_id)},
            {"$set": {
                "title": title,
                "price": float(price),
                "variants": [{"sku": sku, "attrs": {}, "price": float(price)}],
                "categoryId": category_id
            }}
        )
        flash("商品已更新", "success")
        return redirect(url_for("admin_products"))
    categories = list(db.categories.find({}))
    return render_template("admin_edit_product.html", product=product, categories=categories)

@app.route("/admin/products/delete/<product_id>")
@login_required
@admin_required
def admin_delete_product(product_id):
    db.products.delete_one({"_id": ObjectId(product_id)})
    flash("商品已删除", "info")
    return redirect(url_for("admin_products"))

# ======== 类别：任意层插入 + 检索 ========
def _build_tree_push_update(path_ids, new_node):
    """
    path_ids: 从树的第一层节点开始到“父节点”的一串 subcategory _id 列表（不含根文档 id）。
    返回 update_spec, array_filters, update_path
    """
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

@app.route("/categories", methods=["GET"])
def categories_home():
    nodes = list(db.categories.find({}))
    trees = list(db.categories_tree.find({}))
    return render_template("categories.html", nodes=nodes, trees=trees)

@app.route("/categories/add", methods=["POST"])
@login_required
@admin_required
def categories_add():
    name = request.form["name"].strip()
    parent_id_str = request.form.get("parentId", "").strip()
    parent_id = ObjectId(parent_id_str) if parent_id_str else None

    # 邻接表：插入节点并计算 path/pathIds
    if parent_id:
        parent = db.categories.find_one({"_id": parent_id})
        if not parent:
            flash("父类别不存在", "error")
            return redirect(url_for("categories_home"))
        path_ids = parent.get("pathIds", []) + [parent["_id"]]
        path = (parent.get("path") or parent["name"]) + ">" + name
    else:
        path_ids = []
        path = name

    new_id = db.categories.insert_one({
        "name": name,
        "parentId": parent_id,
        "pathIds": path_ids,  # 不包含自身
        "path": path,
        "createdAt": datetime.utcnow(),
    }).inserted_id

    # 同步到树文档
    if not parent_id:
        db.categories_tree.insert_one({
            "_id": new_id, "name": name, "subcategories": [], "createdAt": datetime.utcnow()
        })
    else:
        root_id = path_ids[0] if path_ids else parent_id
        if not db.categories_tree.find_one({"_id": root_id}):
            root_doc = db.categories.find_one({"_id": root_id})
            db.categories_tree.insert_one({"_id": root_id, "name": root_doc["name"], "subcategories": [], "createdAt": datetime.utcnow()})
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
        flash("未找到该类别", "error")
        return redirect(url_for("index"))
    all_ids = res[0]["allIds"]
    products = list(db.products.find({"categoryId": {"$in": all_ids}}))
    return render_template("products_by_category.html", products=products, count=len(products))

# ======== 种子数据（可选）========
def seed_if_empty():
    if db.products.count_documents({}) == 0:
        db.products.insert_many([
            {"title": "Smartphone X", "price": 699.0, "status": "active",
             "variants": [{"sku": "X-BLK-128", "attrs": {"color": "Black", "size": "128G"}, "price": 699.0}],
             "createdAt": datetime.utcnow()},
            {"title": "Laptop Pro", "price": 1299.0, "status": "active",
             "variants": [{"sku": "LP-16-512", "attrs": {"ram": "16G", "ssd": "512G"}, "price": 1299.0}],
             "createdAt": datetime.utcnow()},
        ])
    if db.categories.count_documents({}) == 0:
        root_id = db.categories.insert_one({"name": "Electronics", "parentId": None, "pathIds": [], "path": "Electronics", "createdAt": datetime.utcnow()}).inserted_id
        phone_id = db.categories.insert_one({"name": "Phones", "parentId": root_id, "pathIds": [root_id], "path": "Electronics>Phones", "createdAt": datetime.utcnow()}).inserted_id
        db.categories_tree.insert_one({"_id": root_id, "name": "Electronics", "subcategories": [{"_id": phone_id, "name": "Phones", "subcategories": []}], "createdAt": datetime.utcnow()})

# ======== 辅助：快速把用户设为管理员 ========
@app.route("/dev/make_admin/<email>")
def dev_make_admin(email):
    db.users.update_one({"email": email.lower()}, {"$set": {"isAdmin": True}})
    return "OK"

if __name__ == "__main__":
    with app.app_context():
        seed_if_empty()
    app.run(debug=True)
