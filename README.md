# Flask + MongoDB Mini E-commerce

## Features
- User registration / login / logout (password-less demo)
- Product CRUD (admin-only)
- Cart & checkout
- Categories: add at any depth (keeps both a nested tree and an adjacency list)
- Browse products by category (includes all descendants)

## Setup & Run

```bash
# 1) Create and activate a Conda env (Python 3.13)
conda create -n db-project python=3.13
conda activate db-project

# 2) Install dependencies
pip install flask flask-login pymongo python-dotenv

# 3) Install MongodbMongoDB Community version

# 4) Start the app
python flask_ecommerce_novalidate.py
```

- Open http://127.0.0.1:5000/
- Register an account, then visit:
  ```
  http://127.0.0.1:5000/dev/make_admin/<your_email>
  ```
  to promote it to admin.
- Use the navigation to access **Admin (Products)** and **Categories (Manage/Search)**.

