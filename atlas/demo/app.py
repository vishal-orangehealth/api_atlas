"""Atlas Shop: a tiny demo app to try API Atlas on.

Run with `atlas demo`, then explore http://127.0.0.1:8765 from API Atlas.
Flow: register -> log in -> browse products -> add to cart -> place order -> view order.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Atlas Shop (demo)")

USERS: dict[str, dict] = {}       # email -> user
TOKENS: dict[str, str] = {}       # token -> email
CARTS: dict[str, dict] = {}       # email -> cart
ORDERS: dict[str, dict] = {}      # orderId -> order
PRODUCTS = [
    {"id": 1, "name": "Mechanical keyboard", "price": 4499, "stock": 12, "category": "accessories"},
    {"id": 2, "name": "USB-C hub", "price": 1999, "stock": 30, "category": "accessories"},
    {"id": 3, "name": "27\" monitor", "price": 15999, "stock": 5, "category": "displays"},
    {"id": 4, "name": "Noise-cancelling headphones", "price": 8999, "stock": 8, "category": "audio"},
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RegisterIn(BaseModel):
    name: str = Field(min_length=1)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=6)


class LoginIn(BaseModel):
    email: str
    password: str


class CartItemIn(BaseModel):
    productId: int
    quantity: int = Field(ge=1, le=10)


class OrderIn(BaseModel):
    cartId: str
    paymentMethod: str = Field(pattern=r"^(card|upi|cod)$")
    address: str = Field(min_length=5)


def current_user(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")
    email = TOKENS.get(authorization.removeprefix("Bearer "))
    if not email:
        raise HTTPException(401, "Invalid token")
    return USERS[email]


def public_user(u: dict) -> dict:
    return {"id": u["id"], "name": u["name"], "email": u["email"], "createdAt": u["createdAt"]}


@app.post("/api/auth/register", status_code=201)
def register(body: RegisterIn):
    if body.email in USERS:
        raise HTTPException(409, "Email already registered")
    u = {"id": len(USERS) + 1001, "name": body.name, "email": body.email, "password": body.password,
         "createdAt": now()}
    USERS[body.email] = u
    return public_user(u)


@app.post("/api/auth/login")
def login(body: LoginIn):
    u = USERS.get(body.email)
    if not u or u["password"] != body.password:
        raise HTTPException(401, "Wrong email or password")
    token = secrets.token_urlsafe(24)
    TOKENS[token] = body.email
    return {"token": token, "tokenType": "Bearer", "expiresIn": 3600, "user": public_user(u)}


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    return public_user(user)


@app.get("/api/products")
def products(category: str | None = None):
    items = [p for p in PRODUCTS if not category or p["category"] == category]
    return {"items": items, "total": len(items)}


@app.get("/api/products/{product_id}")
def product(product_id: int):
    for p in PRODUCTS:
        if p["id"] == product_id:
            return p
    raise HTTPException(404, "Product not found")


def _cart(user: dict) -> dict:
    return CARTS.setdefault(user["email"], {"cartId": str(uuid.uuid4()), "items": [], "total": 0})


@app.post("/api/cart/items", status_code=201)
def add_to_cart(body: CartItemIn, user: dict = Depends(current_user)):
    p = next((p for p in PRODUCTS if p["id"] == body.productId), None)
    if not p:
        raise HTTPException(404, "Product not found")
    cart = _cart(user)
    cart["items"].append({"productId": p["id"], "name": p["name"], "quantity": body.quantity,
                          "price": p["price"]})
    cart["total"] = sum(i["price"] * i["quantity"] for i in cart["items"])
    return cart


@app.get("/api/cart")
def get_cart(user: dict = Depends(current_user)):
    return _cart(user)


@app.post("/api/orders", status_code=201)
def place_order(body: OrderIn, user: dict = Depends(current_user)):
    cart = _cart(user)
    if body.cartId != cart["cartId"]:
        raise HTTPException(404, "Cart not found")
    if not cart["items"]:
        raise HTTPException(400, "Cart is empty")
    oid = str(uuid.uuid4())
    order = {"orderId": oid, "status": "PLACED", "total": cart["total"], "items": cart["items"],
             "paymentMethod": body.paymentMethod, "address": body.address, "createdAt": now(),
             "owner": user["email"]}
    ORDERS[oid] = order
    CARTS.pop(user["email"], None)
    return {k: v for k, v in order.items() if k != "owner"}


@app.get("/api/orders/{order_id}")
def get_order(order_id: str, user: dict = Depends(current_user)):
    o = ORDERS.get(order_id)
    if not o or o["owner"] != user["email"]:
        raise HTTPException(404, "Order not found")
    return {k: v for k, v in o.items() if k != "owner"}


@app.post("/api/telemetry/rum", status_code=204)
def rum():
    return None


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Atlas Shop</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{font-family:system-ui,sans-serif;max-width:760px;margin:24px auto;padding:0 16px;color:#1d2433;background:#f6f7f9}
 h1{font-size:22px} section{background:#fff;border:1px solid #e3e6ec;border-radius:10px;padding:16px;margin:12px 0}
 input,select,button{font:inherit;padding:8px 10px;border-radius:7px;border:1px solid #cfd5df;margin:4px 4px 4px 0}
 button{background:#2f5bea;color:#fff;border-color:#2f5bea;cursor:pointer} button:disabled{opacity:.5}
 .p{display:flex;justify-content:space-between;align-items:center;border-top:1px solid #eef0f4;padding:8px 0}
 #log{font:12px ui-monospace,monospace;white-space:pre-wrap;color:#55607a}
</style></head><body>
<h1>🛒 Atlas Shop <small style="font-weight:400;color:#7a849a">demo app for API Atlas</small></h1>
<section id="auth"><b>1. Account</b><br>
 <input id="name" placeholder="Name" value="Vishal">
 <input id="email" placeholder="Email">
 <input id="password" type="password" placeholder="Password" value="secret123">
 <br><button id="register">Register</button><button id="login">Log in</button>
 <span id="who"></span></section>
<section><b>2. Products</b> <select id="cat"><option value="">All</option><option>accessories</option><option>displays</option><option>audio</option></select>
 <div id="products"></div></section>
<section><b>3. Cart</b> <div id="cart">Log in and add something.</div>
 <input id="address" placeholder="Address" value="221B MG Road, Bengaluru">
 <select id="pay"><option>card</option><option>upi</option><option>cod</option></select>
 <button id="order">Place order</button></section>
<section><b>4. Order</b> <div id="orderbox">No order yet.</div></section>
<div id="log"></div>
<script>
let token=null, cartId=null;
document.getElementById('email').value='user'+Math.floor(Math.random()*1e6)+'@example.com';
const log=m=>document.getElementById('log').textContent=m;
async function call(method,url,body){
  const h={'Content-Type':'application/json'}; if(token) h['Authorization']='Bearer '+token;
  const r=await fetch(url,{method,headers:h,body:body?JSON.stringify(body):undefined});
  const t=await r.text(); let j=null; try{j=JSON.parse(t)}catch(e){}
  log(method+' '+url+' -> '+r.status); if(!r.ok) throw new Error(t); return j;
}
const v=id=>document.getElementById(id).value;
document.getElementById('register').onclick=async()=>{await call('POST','/api/auth/register',{name:v('name'),email:v('email'),password:v('password')});};
document.getElementById('login').onclick=async()=>{const r=await call('POST','/api/auth/login',{email:v('email'),password:v('password')});
  token=r.token; const me=await call('GET','/api/me'); document.getElementById('who').textContent='Hi '+me.name;};
async function loadProducts(){const c=v('cat'); const r=await call('GET','/api/products'+(c?'?category='+c:''));
  document.getElementById('products').innerHTML=r.items.map(p=>`<div class="p"><span>${p.name} — ₹${p.price}</span>
   <span><button data-view="${p.id}" style="background:#fff;color:#2f5bea">View</button><button data-add="${p.id}">Add to cart</button></span></div>`).join('');}
document.getElementById('cat').onchange=loadProducts;
document.getElementById('products').onclick=async e=>{
  const add=e.target.dataset.add, view=e.target.dataset.view;
  if(view){const p=await call('GET','/api/products/'+view); log(p.name+': '+p.stock+' in stock');}
  if(add){const c=await call('POST','/api/cart/items',{productId:Number(add),quantity:1}); cartId=c.cartId;
    document.getElementById('cart').textContent=c.items.length+' item(s), total ₹'+c.total;}};
document.getElementById('order').onclick=async()=>{const o=await call('POST','/api/orders',{cartId,paymentMethod:v('pay'),address:v('address')});
  const full=await call('GET','/api/orders/'+o.orderId); document.getElementById('orderbox').textContent='Order '+full.orderId+' is '+full.status;};
loadProducts(); fetch('/api/telemetry/rum',{method:'POST',body:'{}'});
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


def reset() -> None:
    USERS.clear(); TOKENS.clear(); CARTS.clear(); ORDERS.clear()
