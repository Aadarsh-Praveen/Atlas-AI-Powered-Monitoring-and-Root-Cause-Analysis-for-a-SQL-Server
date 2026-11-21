"""Pricing rules shared by the cart and checkout.

The ONE place the shipping rule lives. The cart uses it for its estimate and
checkout uses it for the real order, so the two can never disagree, and the
storefront just displays the result (it never calculates money).

The same rule is used for the historical orders in shoplite/db/seed.py
(FREE_SHIPPING_FROM_CENTS, SHIPPING_CENTS); keep them in step.
"""

from decimal import Decimal

FREE_SHIPPING_FROM = Decimal("75.00")   # orders at or above this ship free
SHIPPING_COST = Decimal("7.99")         # otherwise, a flat rate
ZERO = Decimal("0.00")


def shipping_cost_for(subtotal: Decimal) -> Decimal:
    """Shipping for an order with this subtotal: free from $75, else $7.99."""
    return ZERO if subtotal >= FREE_SHIPPING_FROM else SHIPPING_COST
