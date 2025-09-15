# Flask + MongoDB 简易电商（无 Schema 校验）

## 功能
- 用户注册/登录/登出（无密码版演示）
- 商品 CRUD（后台，管理员可见）
- 购物车、下单
- 类别：任意层级添加（树结构同步 & 邻接表）
- 按类别（含子孙）检索商品

## 运行
```bash
pip install flask flask-login pymongo python-dotenv
# 可选：连接串
# echo "MONGODB_URI=mongodb://localhost:27017" > .env
python flask_ecommerce_novalidate.py
```

- 打开 http://127.0.0.1:5000/
- 注册一个账号后，访问 http://127.0.0.1:5000/dev/make_admin/<你的邮箱> 将其设为管理员
- 导航进入「后台管理」与「类别管理/检索」

## 说明
- 无任何 Schema 校验；金额以 float 存储，仅为演示。
- `categories` 为邻接表（便于查询），`categories_tree` 为嵌套树（演示 `$push`/`arrayFilters`）。
- 商品的 `categoryId` 指向 `categories._id`，检索时使用 `$graphLookup` 获取子树所有节点。
