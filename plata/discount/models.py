from datetime import date, datetime
from decimal import Decimal
import random
import string

from django import forms
from django.contrib.admin.widgets import FilteredSelectMultiple
from django.core.exceptions import ValidationError
from django.core.urlresolvers import get_callable
from django.db import models
from django.db.models import Q
from django.utils.translation import ugettext_lazy as _

import plata
from plata.fields import CurrencyField
from plata.product.models import Category, Product, ProductVariation
from plata.shop.processors import ProcessorBase
from plata.utils import JSONFieldDescriptor


class DiscountBase(models.Model):
    AMOUNT_EXCL_TAX = 10
    AMOUNT_INCL_TAX = 20
    PERCENTAGE = 30

    TYPE_CHOICES = (
        (AMOUNT_EXCL_TAX, _('amount excl. tax')),
        (AMOUNT_INCL_TAX, _('amount incl. tax')),
        (PERCENTAGE, _('percentage')),
        )

    # You can add and remove options at will, except for 'all': This option
    # must always be available, and it cannot have any form fields
    CONFIG_OPTIONS = [
        ('all', {
            'title': _('All products'),
            }),
        ('exclude_sale', {
            'title': _('Exclude sale prices'),
            'orderitem_query': lambda **values: Q(is_sale=False),
            }),
        ('products', {
            'title': _('Explicitly define discountable products'),
            'form_fields': [
                ('products', forms.ModelMultipleChoiceField(
                    Product.objects.all(),
                    label=_('products'),
                    required=True,
                    widget=FilteredSelectMultiple(
                        verbose_name=_('products'),
                        is_stacked=False,
                        ),
                    )),
                ],
            'variation_query': lambda products: Q(product__in=products),
            }),
        ('only_categories', {
            'title': _('Only products from selected categories'),
            'form_fields': [
                ('categories', forms.ModelMultipleChoiceField(
                    Category.objects.all(),
                    label=_('categories'),
                    required=True,
                    widget=FilteredSelectMultiple(
                        verbose_name=_('categories'),
                        is_stacked=False,
                        ),
                    )),
                ],
            'variation_query': lambda categories: Q(product__categories__in=categories),
            }),
        ]

    name = models.CharField(_('name'), max_length=100)

    # TODO currency handling. Maybe split type/value into amount, tax, currency, percentage?
    type = models.PositiveIntegerField(_('type'), choices=TYPE_CHOICES)
    value = models.DecimalField(_('value'), max_digits=18, decimal_places=10)

    config_json = models.TextField(_('configuration'), blank=True,
        help_text=_('If you edit this field directly, changes below will be ignored.'))
    config = JSONFieldDescriptor('config_json')

    class Meta:
        abstract = True

    def __unicode__(self):
        return self.name

    def eligible_products(self, order, items, products=None):
        """
        Return a list of products which are eligible for discounting using
        the discount configuration.
        """

        if not products:
            shop = plata.shop_instance()
            products = shop.product_model._default_manager.all()

        variations = ProductVariation.objects.filter(
            id__in=[item.variation_id for item in items])
        orderitems = shop.orderitem_model.objects.filter(
            id__in=[item.id for item in items])

        for key, parameters in self.config.items():
            parameters = dict((str(k), v) for k, v in parameters.items())

            cfg = dict(self.CONFIG_OPTIONS)[key]

            if 'variation_query' in cfg:
                variations = variations.filter(cfg['variation_query'](**parameters))
            if 'orderitem_query' in cfg:
                orderitems = orderitems.filter(cfg['orderitem_query'](**parameters))

        return products.filter(id__in=variations.values('product_id')).filter(id__in=orderitems.values('variation__product__id'))

    def apply(self, order, items, **kwargs):
        if not items:
            return

        if self.type == self.AMOUNT_EXCL_TAX:
            self.apply_amount_discount(order, items, tax_included=False)
        elif self.type == self.AMOUNT_INCL_TAX:
            self.apply_amount_discount(order, items, tax_included=True)
        elif self.type == self.PERCENTAGE:
            self.apply_percentage_discount(order, items)
        else:
            raise NotImplementedError, 'Unknown discount type %s' % self.type

    def apply_amount_discount(self, order, items, tax_included):
        eligible_products = self.eligible_products(order, items).values_list('id', flat=True)
        eligible_items = [item for item in items if item.variation.product_id in eligible_products]

        if tax_included:
            # calculate mean order item tax rate (only relevant if there are products
            # with different tax rates in the order)
            dividend = divisor = Decimal('0.00')
            for item in eligible_items:
                dividend += item.tax_class.rate * item.discounted_subtotal_excl_tax
                divisor += item.discounted_subtotal_excl_tax

            tax_rate = dividend / divisor
            discount = self.value / (1 + tax_rate/100)
        else:
            discount = self.value

        items_subtotal = sum([item.discounted_subtotal_excl_tax for item in eligible_items],
            Decimal('0.00'))

        # Don't allow bigger discounts than the items subtotal
        if discount > items_subtotal:
            self.remaining = discount - items_subtotal
            self.save()
            discount = items_subtotal

        for item in eligible_items:
            item._line_item_discount += item.discounted_subtotal_excl_tax / items_subtotal * discount

    def apply_percentage_discount(self, order, items):
        eligible_products = self.eligible_products(order, items).values_list('id', flat=True)

        factor = self.value / 100

        for item in items:
            if item.variation.product_id not in eligible_products:
                continue

            item._line_item_discount += item.discounted_subtotal_excl_tax * factor


# Nearly all letters and digits, excluding those which can be easily confounded
RANDOM_CODE_CHARACTERS = '23456789abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ'

def generate_random_code():
    return u''.join(random.sample(RANDOM_CODE_CHARACTERS, 10))


class Discount(DiscountBase):
    code = models.CharField(_('code'), max_length=30, unique=True,
        default=generate_random_code)

    is_active = models.BooleanField(_('is active'), default=True)
    valid_from = models.DateField(_('valid from'), default=date.today)
    valid_until = models.DateField(_('valid until'), blank=True, null=True)

    allowed_uses = models.IntegerField(_('number of allowed uses'),
        blank=True, null=True,
        help_text=_('Leave empty if there is no limit on the number of uses of this discount.'))
    used = models.IntegerField(_('number of times already used'), default=0)

    class Meta:
        verbose_name = _('discount')
        verbose_name_plural = _('discounts')

    def validate(self, order):
        messages = []
        if not self.is_active:
            messages.append(_('Discount is inactive.'))

        today = date.today()
        if today < self.valid_from:
            messages.append(_('Discount is not active yet.'))
        if self.valid_until and today > self.valid_until:
            messages.append(_('Discount is expired.'))

        if self.allowed_uses and self.used >= self.allowed_uses:
            messages.append(_('Allowed uses for this discount has already been reached.'))

        if messages:
            raise ValidationError(messages)

        return True
