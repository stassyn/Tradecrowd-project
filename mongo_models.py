import mongoengine


class FixTradeMsg(mongoengine.Document):
    WAY_IN = 0
    WAY_OUT = 1
    DIRECTIONS = (
        (WAY_IN, 'In'),
        (WAY_OUT, 'Out')
    )
    way = mongoengine.IntField(choices=DIRECTIONS)
    name = mongoengine.StringField()
    body = mongoengine.StringField()
    message = mongoengine.DictField()
    date = mongoengine.DateTimeField()


class ChartHistory(mongoengine.Document):
    TYPE_M1 = 1
    TYPE_M5 = 2
    TYPE_M15 = 3
    TYPE_M30 = 4
    TYPE_H1 = 5
    TYPE_H4 = 6
    TYPE_DAILY = 7
    TYPE_WEEKLY = 8
    TYPE_MONTHLY = 9

    TYPES = (
        (TYPE_M1, '1 min'),
        (TYPE_M5, '5 min'),
        (TYPE_M15, '15 min'),
        (TYPE_M30, '30 min'),
        (TYPE_H1, '1 hour'),
        (TYPE_H4, '4 hour'),
        (TYPE_DAILY, 'Daily'),
        (TYPE_WEEKLY, 'Weekly'),
        (TYPE_MONTHLY, 'Monthly'),
    )
    instrument = mongoengine.StringField()
    type = mongoengine.IntField(choices=TYPES)
    open = mongoengine.FloatField()
    high = mongoengine.FloatField()
    low = mongoengine.FloatField()
    close = mongoengine.FloatField()
    date = mongoengine.DateTimeField()