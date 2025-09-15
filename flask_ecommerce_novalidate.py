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


def slugify(name: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-")

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
            "createdAt": datetime.utcnow(),
            "typeIds": type_ids
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
        # 多类别：checkbox name=categoryIds
        category_ids = []
        for cid in request.form.getlist("categoryIds"):
            try:
                category_ids.append(ObjectId(cid))
            except Exception:
                pass
        # 多类型标签：checkbox name=typeIds
        type_ids = []
        for tid in request.form.getlist("typeIds"):
            try:
                type_ids.append(ObjectId(tid))
            except Exception:
                pass
        db.products.insert_one({
            "title": title,
            "price": float(price),
            "status": "active",
            "variants": [{"sku": sku, "attrs": {}, "price": float(price)}],
            "categoryIds": category_ids,
            "createdAt": datetime.utcnow(),
            "typeIds": type_ids
        })
        flash("商品已添加", "success")
        return redirect(url_for("admin_products"))
    categories = list(db.categories.find({}))
    types = list(db.types.find({}))
    return render_template("admin_add_product.html", categories=categories, types=types)

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
        type_ids = []
        for tid in request.form.getlist("typeIds"):
            try:
                type_ids.append(ObjectId(tid))
            except Exception:
                pass
        db.products.update_one(
            {"_id": ObjectId(product_id)},
            {"$set": {
                "title": title,
                "price": float(price),
                "variants": [{"sku": sku, "attrs": {}, "price": float(price)}],
                "categoryIds": category_ids,
                "typeIds": type_ids
            }}
        )
        flash("商品已更新", "success")
        return redirect(url_for("admin_products"))
    categories = list(db.categories.find({}))
    types = list(db.types.find({}))
    return render_template("admin_edit_product.html", product=product, categories=categories, types=types)

@app.route("/admin/products/delete/<product_id>")
@login_required
@admin_required
def admin_delete_product(product_id):
    db.products.delete_one({"_id": ObjectId(product_id)})
    flash("商品已删除", "info")
    return redirect(url_for("admin_products"))


# ======== 后台：类型（多标签）CRUD ========
@app.route("/admin/types")
@login_required
@admin_required
def admin_types():
    types = list(db.types.find())
    return render_template("admin_types.html", types=types)

@app.route("/admin/types/add", methods=["GET", "POST"])
@login_required
@admin_required
def admin_add_type():
    if request.method == "POST":
        name = request.form["name"].strip()
        slug = slugify(name)
        db.types.insert_one({"name": name, "slug": slug, "createdAt": datetime.utcnow()})
        flash("类型已添加", "success")
        return redirect(url_for("admin_types"))
    return render_template("admin_add_type.html")

@app.route("/admin/types/edit/<type_id>", methods=["GET", "POST"])
@login_required
@admin_required
def admin_edit_type(type_id):
    t = db.types.find_one({"_id": ObjectId(type_id)})
    if not t:
        flash("类型不存在", "error")
        return redirect(url_for("admin_types"))
    if request.method == "POST":
        name = request.form["name"].strip()
        slug = slugify(name)
        db.types.update_one({"_id": ObjectId(type_id)}, {"$set": {"name": name, "slug": slug}})
        flash("类型已更新", "success")
        return redirect(url_for("admin_types"))
    return render_template("admin_edit_type.html", t=t)

@app.route("/admin/types/delete/<type_id>")
@login_required
@admin_required
def admin_delete_type(type_id):
    db.types.delete_one({"_id": ObjectId(type_id)})
    # 同步移除产品里的引用（可选）
    db.products.update_many({}, {"$pull": {"typeIds": ObjectId(type_id)}})
    flash("类型已删除", "info")
    return redirect(url_for("admin_types"))


def rebuild_categories_tree():
    """
    依据邻接表 categories 重新构建 categories_tree（整棵树重建，简单可靠）。
    - 对每个根(parentId=None)创建一棵树文档
    - 递归填充 subcategories
    """
    db.categories_tree.delete_many({})
    nodes = list(db.categories.find({}))
    by_parent = {}
    by_id = {}
    for n in nodes:
        by_id[n["_id"]] = n
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
        tree_doc = {
            "_id": root["_id"],
            "name": root.get("name"),
            "subcategories": build_children(root["_id"]),
            "createdAt": datetime.utcnow()
        }
        db.categories_tree.insert_one(tree_doc)
    return True

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




# ======== 后台：类别管理（编辑/列表入口） ========
@app.route("/admin/categories")
@login_required
@admin_required
def admin_categories():
    nodes = list(db.categories.find({}))
    # id -> name 映射，只取直接父名称
    id2name = {str(n["_id"]): n.get("name") for n in nodes}
    for n in nodes:
        if n.get("parentId"):
            n["parentName"] = id2name.get(str(n["parentId"]), "(未知)")
        else:
            n["parentName"] = "根"
    return render_template("admin_categories.html", nodes=nodes)


@app.route("/categories/edit/<cat_id>", methods=["GET", "POST"])
@login_required
@admin_required
def categories_edit(cat_id):
    cat = db.categories.find_one({"_id": ObjectId(cat_id)})
    if not cat:
        flash("类别不存在", "error")
        return redirect(url_for("admin_categories"))
    if request.method == "POST":
        new_name = request.form["name"].strip()
        # 1) 更新该节点
        db.categories.update_one({"_id": cat["_id"]}, {"$set": {"name": new_name}})

        # 2) 计算新路径前缀
        if cat.get("parentId"):
            parent = db.categories.find_one({"_id": cat["parentId"]})
            parent_path = parent.get("path") or parent["name"]
            new_prefix = parent_path + ">" + new_name
        else:
            new_prefix = new_name

        old_prefix = cat.get("path", cat["name"])

        # 3) 更新该节点 path
        db.categories.update_one({"_id": cat["_id"]}, {"$set": {"path": new_prefix}})

        # 4) 找到所有后代并逐个修正 path
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

        # 5) 同步树文档里的名字
        # 定位根与链路
        if cat.get("parentId"):
            # 取根 id
            root_id = (cat.get("pathIds") or [cat["_id"]])[0]
            # 构造链（从第一层到目标）：取 cat.pathIds 去掉首个根，再加上自己
            chain_under_root = (cat.get("pathIds", [])[1:] if cat.get("pathIds") else []) + [cat["_id"]]
        else:
            root_id = cat["_id"]
            chain_under_root = []  # 目标就是根文档

        if not db.categories_tree.find_one({"_id": root_id}):
            # 安全兜底：若无树根，重建一个
            root_doc = db.categories.find_one({"_id": root_id})
            db.categories_tree.insert_one({"_id": root_doc["_id"], "name": root_doc["name"], "subcategories": [], "createdAt": datetime.utcnow()})

        if len(chain_under_root) == 0:
            # 目标是根：直接更新根 name
            db.categories_tree.update_one({"_id": root_id}, {"$set": {"name": new_name}})
        else:
            # 构造路径和 arrayFilters，命中目标 .name
            parts, af = [], []
            for i, cid in enumerate(chain_under_root):
                parts += ["subcategories", f"$[lvl{i}]"]
                af.append({f"lvl{i}._id": cid})
            target_path = ".".join(parts + ["name"])
            db.categories_tree.update_one({"_id": root_id}, {"$set": {target_path: new_name}}, array_filters=af)

        flash("类别已更新", "success")
        return redirect(url_for("admin_categories"))

    # GET
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

    # 获取子树全部 id（含自身）
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
        return redirect(url_for("admin_categories"))
    all_ids = res[0]["allIds"]

    # 1) 从邻接表删除整棵子树
    db.categories.delete_many({"_id": {"$in": all_ids}})

    # 2) 同步移除所有商品中的引用
    db.products.update_many({}, {"$pull": {"categoryIds": {"$in": all_ids}}})

    # 3) 重建 categories_tree
    rebuild_categories_tree()

    flash("类别及其子类已删除，商品引用已移除", "info")
    return redirect(url_for("admin_categories"))

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
    products = list(db.products.find({"categoryIds": {"$in": all_ids}}))
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
    if db.types.count_documents({}) == 0:
        db.types.insert_many([
            {"name": "NewArrival", "slug": "newarrival", "createdAt": datetime.utcnow()},
            {"name": "Hot", "slug": "hot", "createdAt": datetime.utcnow()},
            {"name": "OnSale", "slug": "onsale", "createdAt": datetime.utcnow()},
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
