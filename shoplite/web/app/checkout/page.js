"use client";
// app/checkout/page.js: shipping details (prefilled from the customer),
// payment method, place order, and a clear result.
//
// The three outcomes the API can return, each with its own screen:
//   201 + status "paid"            -> order confirmed
//   201 + status "payment_failed"  -> declined: not charged, stock restored
//   409                            -> out of stock: nothing changed
// Before ordering, the summary shows the cart's shipping and estimated total,
// calculated by the API with the same rule checkout charges. The final total
// is the one checkout returns. This page only displays the API's amounts.
//
// Layout from docs/design-reference ("Checkout"): its own slim header and
// footer (same .signin-page frame as /signin, so the site-wide ones are
// hidden), a gold banner, Ship to and Payment panels, and the order summary.
//
// DEMO CARD FIELDS: they only accept the test number 4242 4242 4242 4242 and
// are NEVER sent anywhere. The API only receives the payment method; the
// payment itself is simulated server-side.

import Link from "next/link";
import { useEffect, useState } from "react";

import { ErrorNotice, Loading } from "../../components/Notice";
import ProductPhoto from "../../components/ProductPhoto";
import { getCart, getCustomer, placeOrder } from "../../lib/api";
import { formatMoney, formatShipping } from "../../lib/format";

const PAYMENT_METHODS = [
  { value: "card", label: "Credit or debit card" },
  { value: "paypal", label: "PayPal" },
  { value: "gift_card", label: "ShopLite gift card" },
];

const EMPTY_CARD = { number: "", expiry: "", cvc: "", name: "" };

// The page frame from the design: slim header, content, one-line footer.
function CheckoutFrame({ children }) {
  return (
    <div className="signin-page checkout-page">
      <header className="signin-header">
        <div className="signin-shell signin-header-inner">
          <Link href="/" className="signin-brand" aria-label="ShopLite Hardware Supply, home">
            <span className="signin-mark" aria-hidden="true">S</span>
            <span>SHOP<strong>LITE</strong><small>HARDWARE SUPPLY</small></span>
          </Link>
          <Link className="signin-back" href="/">Back to shop</Link>
        </div>
      </header>
      <div className="checkout-main signin-shell">{children}</div>
      <footer className="signin-footer">
        <div className="signin-shell">ShopLite demo store · No payments or shipments</div>
      </footer>
    </div>
  );
}

function CheckoutHero({ title, text, pill = "Demo store" }) {
  return (
    <section className="checkout-hero" aria-labelledby="checkout-title">
      <div>
        <h1 id="checkout-title">{title}</h1>
        {text && <p>{text}</p>}
      </div>
      <span className="checkout-pill">{pill}</span>
    </section>
  );
}

export default function CheckoutPage() {
  const [signedIn, setSignedIn] = useState(null);
  const [cart, setCart] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [shipping, setShipping] = useState(null);
  const [paymentMethod, setPaymentMethod] = useState("card");
  const [card, setCard] = useState(EMPTY_CARD); // stays in this page only
  const [placing, setPlacing] = useState(false);
  const [orderError, setOrderError] = useState(null);
  const [result, setResult] = useState(null);

  useEffect(() => {
    const customer = getCustomer();
    setSignedIn(Boolean(customer));
    if (!customer) return;
    // Prefill from the customer on file; the shopper can change any field.
    setShipping({
      name: `${customer.first_name} ${customer.last_name}`,
      address_line1: customer.address_line1,
      city: customer.city,
      state: customer.state,
      postal_code: customer.postal_code,
    });
    getCart().then(setCart).catch(setLoadError);
  }, []);

  const setField = (field) => (event) => setShipping((s) => ({ ...s, [field]: event.target.value }));
  const setCardField = (field) => (event) => setCard((c) => ({ ...c, [field]: event.target.value }));

  function fillDemoCard() {
    setCard({
      number: "4242 4242 4242 4242",
      expiry: `12/${String(new Date().getFullYear() + 2).slice(-2)}`,
      cvc: "123",
      name: shipping.name.trim() || "Demo Customer",
    });
  }

  async function handlePlaceOrder(event) {
    event.preventDefault();
    setPlacing(true); // also prevents a double click from submitting twice
    setOrderError(null);
    try {
      // Only the method and address go to the API; the card fields don't.
      const order = await placeOrder({
        payment_method: paymentMethod,
        shipping: { ...shipping, state: shipping.state.trim().toUpperCase() },
      });
      setCard(EMPTY_CARD);
      setResult(order);
    } catch (err) {
      setOrderError(err);
      // Stock may have changed: reload the cart so the summary is current.
      getCart().then(setCart).catch(() => {});
    } finally {
      setPlacing(false);
    }
  }

  // --- Result screen -------------------------------------------------------
  // The receipt card from docs/design-reference ("Order confirmation"): a
  // gold top with a tick (or a red "!" when the payment was declined), the
  // order number and total, then View order / Keep shopping.
  if (result) {
    const paid = result.status === "paid";
    return (
      <CheckoutFrame>
        <article className="receipt" aria-labelledby="receipt-title"
                 data-testid="order-result" data-status={result.status}>
          <div className={paid ? "receipt-intro" : "receipt-intro receipt-intro-declined"}>
            <span className={paid ? "receipt-check" : "receipt-check receipt-check-declined"} aria-hidden="true">
              {paid ? "✓" : "!"}
            </span>
            <h1 id="receipt-title">{paid ? "Order confirmed" : "Payment declined"}</h1>
            {/* The API's own words, e.g. "Thanks! Your order is confirmed." */}
            <p data-testid="order-result-message">{result.message}</p>
          </div>
          <div className="receipt-content">
            <dl>
              <div><dt>Order number</dt><dd>#{result.order_id}</dd></div>
              <div className="receipt-total">
                <dt>{paid ? "Order total" : "Order total (not charged)"}</dt>
                <dd>{formatMoney(result.total)}</dd>
              </div>
            </dl>
            <div className="receipt-actions">
              <Link className="receipt-button receipt-primary" href={`/account/orders/${result.order_id}`}>
                {paid ? `View order #${result.order_id}` : "Open the order to reorder"}
              </Link>
              <Link className="receipt-button" href="/">Keep shopping</Link>
            </div>
          </div>
        </article>
      </CheckoutFrame>
    );
  }

  // --- Before the order is placed -------------------------------------------
  if (signedIn === false) {
    return (
      <CheckoutFrame>
        <CheckoutHero title="Checkout" text="Sign in to check out." />
        <section className="checkout-panel">
          <p><Link href="/signin">Sign in</Link> to see your cart and place an order.</p>
        </section>
      </CheckoutFrame>
    );
  }
  if (loadError) {
    return (
      <CheckoutFrame>
        <ErrorNotice error={loadError} title="We couldn’t load your cart" />
      </CheckoutFrame>
    );
  }
  if (!cart || !shipping) return <CheckoutFrame><Loading label="Preparing checkout" /></CheckoutFrame>;

  if (cart.items.length === 0) {
    return (
      <CheckoutFrame>
        <CheckoutHero title="Nothing to check out" text="Your cart is empty." />
        <section className="checkout-panel" data-testid="cart-empty">
          <p><Link href="/">Browse the aisles</Link> to find what you need.</p>
        </section>
      </CheckoutFrame>
    );
  }

  const outOfStock = orderError?.status === 409;
  const payingByCard = paymentMethod === "card";

  return (
    <CheckoutFrame>
      <CheckoutHero title="Checkout" text="Review your shipping details and order total." />

      <form className="checkout-layout" onSubmit={handlePlaceOrder}>
        <div className="checkout-fields">
          <section className="checkout-panel" aria-labelledby="shipping-title">
            <h2 id="shipping-title">Ship to</h2>
            <div className="checkout-shipping">
              <div>
                <label htmlFor="ship-name">Full name</label>
                <input id="ship-name" required maxLength={200} autoComplete="name"
                       value={shipping.name} onChange={setField("name")} data-testid="shipping-name-input" />
              </div>
              <div>
                <label htmlFor="ship-address">Street address</label>
                <input id="ship-address" required maxLength={200} autoComplete="address-line1"
                       value={shipping.address_line1} onChange={setField("address_line1")}
                       data-testid="shipping-address-input" />
              </div>
              <div className="checkout-location">
                <div className="checkout-city">
                  <label htmlFor="ship-city">City</label>
                  <input id="ship-city" required maxLength={100} autoComplete="address-level2"
                         value={shipping.city} onChange={setField("city")} data-testid="shipping-city-input" />
                </div>
                <div>
                  <label htmlFor="ship-state">State</label>
                  {/* Two-letter US state code; the API checks the same rule. */}
                  <input id="ship-state" required maxLength={2} pattern="[A-Za-z]{2}"
                         title="Enter a two-letter state code" autoComplete="address-level1"
                         value={shipping.state} onChange={setField("state")} data-testid="shipping-state-input" />
                </div>
                <div>
                  <label htmlFor="ship-postal">ZIP code</label>
                  <input id="ship-postal" required inputMode="numeric" pattern="\d{5}" maxLength={5}
                         title="Enter a five-digit ZIP code" autoComplete="postal-code"
                         value={shipping.postal_code} onChange={setField("postal_code")}
                         data-testid="shipping-postal-input" />
                </div>
              </div>
            </div>
          </section>

          <section className="checkout-panel" aria-labelledby="payment-title">
            <h2 id="payment-title">Payment</h2>
            <label htmlFor="payment-method">Payment method</label>
            <select id="payment-method" required value={paymentMethod}
                    aria-controls="card-fields" aria-describedby="payment-note"
                    onChange={(e) => setPaymentMethod(e.target.value)} data-testid="payment-method-select">
              {PAYMENT_METHODS.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
            </select>

            {payingByCard && (
              <fieldset className="checkout-card-fields" id="card-fields" aria-label="Card details">
                <ul className="card-brands" aria-label="Card network logos">
                  <li className="card-brand visa"><span role="img" aria-label="Visa">VISA</span></li>
                  <li className="card-brand mastercard"><span className="mastercard-mark" role="img" aria-label="Mastercard" /></li>
                  <li className="card-brand amex"><span role="img" aria-label="American Express">AMERICAN<br />EXPRESS</span></li>
                  <li className="card-brand discover"><span role="img" aria-label="Discover">DISC<b>O</b>VER</span></li>
                </ul>
                <p className="checkout-note" id="demo-card-note">
                  Use demo card details only. This checkout simulates payments.
                </p>
                <div>
                  <label htmlFor="card-number">Card number</label>
                  <input id="card-number" inputMode="numeric" autoComplete="off" placeholder="1234 5678 9012 3456"
                         maxLength={19} pattern="4242 ?4242 ?4242 ?4242"
                         title="Use the demo card number 4242 4242 4242 4242"
                         aria-describedby="demo-card-note" required
                         value={card.number} onChange={setCardField("number")} data-testid="card-number-input" />
                </div>
                <div className="checkout-card-row">
                  <div>
                    <label htmlFor="card-expiry">Expiry date</label>
                    <input id="card-expiry" inputMode="numeric" autoComplete="off" placeholder="MM/YY" maxLength={5}
                           pattern="(0[1-9]|1[0-2])/[0-9]{2}" title="Enter a date in MM/YY format" required
                           value={card.expiry} onChange={setCardField("expiry")} data-testid="card-expiry-input" />
                  </div>
                  <div>
                    <label htmlFor="card-cvc">Security code (CVC)</label>
                    <input id="card-cvc" inputMode="numeric" autoComplete="off" placeholder="123" maxLength={3}
                           pattern="[0-9]{3}" title="Enter a three-digit demo security code" required
                           value={card.cvc} onChange={setCardField("cvc")} data-testid="card-cvc-input" />
                  </div>
                </div>
                <div>
                  <label htmlFor="cardholder">Name on card</label>
                  <input id="cardholder" autoComplete="off" placeholder="Full name" required
                         value={card.name} onChange={setCardField("name")} data-testid="card-name-input" />
                </div>
                <button className="checkout-use-demo" type="button" onClick={fillDemoCard}
                        data-testid="use-demo-card">
                  Use demo card
                </button>
              </fieldset>
            )}
            <p id="payment-note" className="checkout-note">
              Payments are simulated. About 1 in 50 demo attempts is declined.
            </p>
          </section>
        </div>

        <section className="checkout-panel" aria-labelledby="summary-title">
          <h2 id="summary-title">Order summary</h2>
          {cart.items.map((item) => (
            <div className="checkout-item" key={item.product_id}>
              <div className="checkout-item-photo"><ProductPhoto product={item} size="thumb" /></div>
              <div>
                <p className="checkout-item-name">{item.name}</p>
                <p className="checkout-note">Qty {item.quantity}</p>
              </div>
              <p className="checkout-price">{formatMoney(item.line_total)}</p>
            </div>
          ))}
          <dl className="checkout-totals">
            <div><dt>Subtotal</dt><dd>{formatMoney(cart.subtotal)}</dd></div>
            <div><dt>Shipping</dt><dd data-testid="checkout-shipping">{formatShipping(cart.shipping_cost)}</dd></div>
            <div className="checkout-total">
              <dt>Estimated total</dt>
              <dd data-testid="checkout-estimated-total">{formatMoney(cart.estimated_total)}</dd>
            </div>
          </dl>
          <button type="submit" className="checkout-place" disabled={placing} data-testid="place-order-button">
            {placing ? "Placing order…" : "Place order"}
          </button>
          <div aria-live="polite">
            {orderError && (
              outOfStock ? (
                <p className="checkout-feedback" data-error="">
                  {orderError.message} Nothing was charged. <Link href="/cart">Review your cart</Link>.
                </p>
              ) : (
                <ErrorNotice error={orderError} title="We couldn’t place your order" />
              )
            )}
          </div>
        </section>
      </form>
    </CheckoutFrame>
  );
}
