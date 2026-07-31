"""
persona.py — the fictional business the seed corpus describes.

Kept separate from seed.py because this is the part a human reads and edits.
It appears in the demo video, so it has to sound like a real support inbox:
no test_customer_1, no lorem ipsum, no "Item A".

Thornbury & Wren is invented. Any resemblance to a real shop is accidental.
"""

BUSINESS = {
    "name": "Thornbury & Wren",
    "trade": "independent homeware — kitchen, table and soft furnishings",
    "setting": "one shop in London plus an online store, three staff",
    "channels": {
        "email": "orders@thornburyandwren.co.uk — longer, more formal, often "
                 "with an order number quoted",
        "whatsapp": "WhatsApp Business — short, lowercase, no greeting, "
                    "sometimes several messages of context in one",
        "webchat": "website chat widget — mid-length, impatient, customer is "
                   "usually looking at the order page as they type",
    },
    "tone": "British English. Customers are direct but rarely rude. Staff "
            "resolve most things generously because the margin on a mug is "
            "worth less than the review.",
}

# Real-sounding names. Deliberately unremarkable — these are on screen.
CUSTOMERS = [
    ("ELLIS-J", "Jess Ellis"), ("OKONKWO-A", "Ada Okonkwo"),
    ("HARTLEY-M", "Michael Hartley"), ("PATEL-R", "Rina Patel"),
    ("DUNCAN-S", "Stuart Duncan"), ("BRENNAN-C", "Ciara Brennan"),
    ("WHITFIELD-L", "Laura Whitfield"), ("OSEI-K", "Kwame Osei"),
    ("MARSDEN-P", "Peter Marsden"), ("NGUYEN-T", "Thu Nguyen"),
    ("CLARKE-H", "Hannah Clarke"), ("ABBOTT-D", "Danny Abbott"),
    ("ROWE-B", "Bethan Rowe"), ("KOWALSKI-M", "Marta Kowalski"),
    ("FENTON-G", "Grace Fenton"), ("IQBAL-S", "Sana Iqbal"),
    ("BAXTER-N", "Neil Baxter"), ("ADEYEMI-F", "Folake Adeyemi"),
    ("SINCLAIR-R", "Robert Sinclair"), ("MURRAY-E", "Eilidh Murray"),
    ("THOMPSON-J", "Joanne Thompson"), ("REYES-C", "Carlos Reyes"),
    ("HOLLAND-A", "Amy Holland"), ("BURKE-D", "Declan Burke"),
    ("SHAH-P", "Priya Shah"), ("WALSH-T", "Tom Walsh"),
    ("LINDQVIST-K", "Karin Lindqvist"), ("OYELARAN-B", "Bisi Oyelaran"),
    ("MCGRATH-S", "Sinead McGrath"), ("PRITCHARD-O", "Owen Pritchard"),
    ("KAUR-J", "Jaspreet Kaur"), ("NORTON-C", "Claire Norton"),
    ("EGAN-M", "Martin Egan"), ("YILMAZ-D", "Deniz Yilmaz"),
    ("BLACKWOOD-R", "Ruth Blackwood"), ("FARRELL-L", "Liam Farrell"),
    ("DASILVA-M", "Mariana da Silva"), ("HEWITT-A", "Alex Hewitt"),
    ("CHEN-W", "Wei Chen"), ("MOONEY-K", "Kate Mooney"),
]

# (item, price in pence). Prices a small homeware shop would actually charge.
ITEMS = [
    ("Stoneware mug, speckled grey", 1400),
    ("Set of four stoneware mugs", 4800),
    ("Linen apron, natural", 2600),
    ("Wool throw, oatmeal", 8500),
    ("Beeswax candle, fig", 1800),
    ("Set of three storage jars", 3200),
    ("Stoneware teapot, 1.2L", 4200),
    ("Cotton tea towels, pair", 1200),
    ("Serving board, ash", 3600),
    ("Ceramic butter dish", 2200),
    ("Enamel jug, cream", 2800),
    ("Recycled glass tumblers, set of six", 3900),
    ("Cushion cover, herringbone", 2400),
    ("Cast iron trivet", 1900),
    ("Stoneware serving bowl, large", 5400),
    ("Table runner, washed linen", 3100),
    ("Scented diffuser, bergamot", 3400),
    ("Espresso cups, set of two", 2100),
]

# What people actually contact a homeware shop about, and roughly how often.
THEMES = [
    ("arrived damaged", 16),
    ("never arrived / tracking stalled", 15),
    ("wrong item or size sent", 12),
    ("part of order missing", 9),
    ("wants to return, needs a label", 9),
    ("refund not yet received", 8),
    ("asking to change delivery address", 6),
    ("wants gift wrapping or a note added", 5),
    ("asking if an item will be restocked", 5),
    ("price dropped after they ordered", 4),
    ("charged twice", 4),
    ("colour not as pictured", 4),
    ("cancel before dispatch", 3),
]

# ---------------------------------------------------------------------------
# Hero cases. Hand-written because they carry the demos and appear on camera.
#
# ORD-4502 is the one the two agents race over: the same duplicate charge
# reported on email and on WhatsApp, minutes apart. Two channels, one order
# row, one refund that must happen exactly once.
# ---------------------------------------------------------------------------

HERO_ORDERS = [
    # (order_id, customer_ref, item, amount_minor, days_ago)
    ("ORD-4502", "ELLIS-J", "Set of four stoneware mugs", 3498, 3),
    ("ORD-4417", "OKONKWO-A", "Wool throw, oatmeal", 8500, 12),
    ("ORD-4390", "HARTLEY-M", "Stoneware teapot, 1.2L", 4200, 18),
    ("ORD-4356", "PATEL-R", "Linen apron, natural", 2600, 24),
]

HERO_CASES = [
    # The race. Same order, same problem, two channels, minutes apart.
    dict(channel="email", customer_ref="ELLIS-J", order_id="ORD-4502",
         subject="Charged twice for order 4502",
         body="I've just checked my statement and order 4502 has gone out "
              "twice — £34.98 on Tuesday and again on Wednesday. I only "
              "placed the one order. Could you refund the second charge?",
         resolution=None, outcome=None, days_ago=3),
    dict(channel="whatsapp", customer_ref="ELLIS-J", order_id="ORD-4502",
         subject="double charged",
         body="hi, think ive been billed twice for the mugs. 34.98 twice on "
              "my card. can you sort it",
         resolution=None, outcome=None, days_ago=3),

    # The discount decision — this is what replay explains.
    dict(channel="webchat", customer_ref="OKONKWO-A", order_id="ORD-4417",
         subject="Throw arrived with a pull in the weave",
         body="The oatmeal throw came with a snag about the size of a 5p "
              "near one corner. It's not unusable but it's not what I paid "
              "£85 for. I'd rather not post it back if I can avoid it.",
         resolution="Offered 20% back as a goodwill discount, customer keeps "
                    "the item. No return required.",
         outcome="goodwill_discount", days_ago=12),

    # Precedent cases the agent should recall when deciding the above.
    dict(channel="email", customer_ref="HARTLEY-M", order_id="ORD-4390",
         subject="Teapot lid chipped in transit",
         body="The teapot arrived with a small chip on the lid rim. Box was "
              "undamaged so I suspect it was packed that way. Happy to keep "
              "it if you can do something on the price.",
         resolution="Refunded 20% and let the customer keep the teapot. "
                    "Cheaper than a return and a replacement.",
         outcome="goodwill_discount", days_ago=18),
    dict(channel="whatsapp", customer_ref="PATEL-R", order_id="ORD-4356",
         subject="apron has a mark on it",
         body="theres a grey mark on the apron pocket, looks like it rubbed "
              "against something. dont really want to send it back for the "
              "sake of it",
         resolution="Gave 20% back, customer kept the apron.",
         outcome="goodwill_discount", days_ago=24),

    # Cross-channel recall: a webchat query should reach these.
    dict(channel="email", customer_ref="DUNCAN-S", order_id=None,
         subject="Order 4288 never turned up",
         body="Tracking hasn't updated in nine days. I've checked with "
              "neighbours and the local depot. Can you chase it or send "
              "another?",
         resolution="Opened a carrier trace and dispatched a replacement "
                    "without waiting for the outcome.",
         outcome="replacement", days_ago=31),
    dict(channel="whatsapp", customer_ref="BRENNAN-C", order_id=None,
         subject="parcel says delivered but its not here",
         body="royal mail says delivered at 11am but theres nothing. ive "
              "looked everywhere",
         resolution="Carrier confirmed a mis-scan. Replacement sent next day.",
         outcome="replacement", days_ago=27),
    dict(channel="webchat", customer_ref="WHITFIELD-L", order_id=None,
         subject="Two of the six tumblers were broken",
         body="Six glass tumblers ordered, two arrived in pieces. The box "
              "looked fine from the outside so I think they needed more "
              "packing.",
         resolution="Sent two replacement tumblers and refunded postage.",
         outcome="partial_replacement", days_ago=21),
    dict(channel="email", customer_ref="OSEI-K", order_id=None,
         subject="Wrong size apron delivered",
         body="I ordered the large linen apron and a small one has arrived. "
              "Order number is 4301. Happy to swap rather than refund.",
         resolution="Correct size sent with a prepaid return label.",
         outcome="exchange", days_ago=15),
    dict(channel="whatsapp", customer_ref="MARSDEN-P", order_id=None,
         subject="refund still not showing",
         body="sent the jug back 2 weeks ago and still no refund. getting a "
              "bit fed up honestly",
         resolution="Refund had failed silently at the payment provider. "
                    "Reprocessed manually and confirmed by email.",
         outcome="refund", days_ago=9),
    dict(channel="webchat", customer_ref="NGUYEN-T", order_id=None,
         subject="Can I add a gift note before it ships?",
         body="Just ordered the candle and storage jars as a housewarming "
              "present — is it too late to add a note saying 'welcome home, "
              "from Thu'?",
         resolution="Note added before dispatch at no charge.",
         outcome="goodwill", days_ago=6),
    dict(channel="email", customer_ref="CLARKE-H", order_id=None,
         subject="Price dropped two days after I ordered",
         body="I bought the ash serving board at £36 and it's now £29 in the "
              "sale. Is there anything you can do?",
         resolution="Refunded the £7 difference as a one-off.",
         outcome="partial_refund", days_ago=11),
]
