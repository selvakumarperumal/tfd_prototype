"""Three documents that disagree on purpose, so the page has something to run.

They are an ordinary order-invoice-delivery trio, not anything domain-specific — the
detector has no idea what a purchase order is, and that is the point.

What is planted:

    Total Amount    50,000.00 on the order, 51,000.00 on the invoice   -> critical
    Seller          'Acme Trading' twice, 'Acme Trading Ltd' once      -> critical
    Delivery Date   promised 2026-03-01, delivered 2026-03-04          -> critical

`Order No`, `Buyer`, `Currency` and `Ship To` agree everywhere, and `Invoice No` and
`Carrier` appear on one document each — so they are skipped rather than reported.
A correct run comes back **blocked**.
"""

SAMPLE: list[dict[str, str]] = [
    {
        'name': 'Purchase order',
        'text': """PURCHASE ORDER
Order No: PO-1042
Buyer: Northwind Imports
Seller: Acme Trading
Currency: USD
Total Amount: 50,000.00
Delivery Date: 2026-03-01
Ship To: Hamburg""",
    },
    {
        'name': 'Invoice',
        'text': """INVOICE
Invoice No: INV-4471
Order No: PO-1042
Buyer: Northwind Imports
Seller: Acme Trading
Currency: USD
Total Amount: 51,000.00
Ship To: Hamburg""",
    },
    {
        'name': 'Delivery note',
        'text': """DELIVERY NOTE
Order No: PO-1042
Buyer: Northwind Imports
Seller: Acme Trading Ltd
Currency: USD
Delivery Date: 2026-03-04
Ship To: Hamburg
Carrier: Pacific Lines""",
    },
]
