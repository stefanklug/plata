from functools import wraps
import logging

from django.contrib import auth, messages
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.core.urlresolvers import get_callable, reverse
from django.forms.models import ModelForm, inlineformset_factory
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.utils.translation import get_language, ugettext as _

import plata
from plata.shop import forms as shop_forms


logger = logging.getLogger('plata.shop.views')


def cart_not_empty(order, shop, request, **kwargs):
    """Redirect to cart if later in checkout process and cart empty"""
    if not order or not order.items.count():
        messages.warning(request, _('Cart is empty.'))
        return shop.redirect('plata_shop_cart')
    
def user_is_authenticated(order, shop, request, **kwargs):
    """ensure the user is authenticated and redirect to checkout if not"""
    if not shop.user_is_authenticated(request.user):
        messages.warning(request, _('You are not authenticated'))
        return shop.redirect('plata_shop_checkout')


def order_already_confirmed(order, shop, request, **kwargs):
    """
    Redirect to confirmation or already paid view if the order is already
    confirmed
    """
    if order and order.status >= order.CONFIRMED:
        if not order.balance_remaining:
            return shop.redirect('plata_order_success')
        messages.warning(request,
            _('You have already confirmed this order earlier, but it is not'
                ' fully paid for yet.'))
        return HttpResponseRedirect(
            shop.reverse_url('plata_shop_confirmation') + '?confirmed=1')


def order_cart_validates(order, shop, request, **kwargs):
    """
    Redirect to cart if stock is insufficient and display an error message
    """
    if request.method != 'GET':
        return

    try:
        order.validate(order.VALIDATE_CART)
    except ValidationError, e:
        for message in e.messages:
            messages.error(request, message)
        return HttpResponseRedirect(
            shop.reverse_url('plata_shop_cart') + '?e=1')


def order_cart_warnings(order, shop, request, **kwargs):
    """Show warnings in cart, but don't redirect (meant as a replacement for
    ``order_cart_validates``, but usable on the cart view itself)"""
    if request.method != 'GET' or request.GET.get('e') or not order:
        return

    try:
        order.validate(order.VALIDATE_CART)
    except ValidationError, e:
        for message in e.messages:
            messages.warning(request, message)


def checkout_process_decorator(*checks):
    """
    Calls all passed checkout process decorators in turn::

        @checkout_process_decorator(order_already_confirmed,
            order_cart_validates)

    All checkout process decorators are called with the order, the shop
    instance and the request as keyword arguments. In the future, additional
    keywords might be added, your decorators should accept ``**kwargs`` as
    well for future compatibility.
    """

    def _dec(fn):
        def _fn(request, *args, **kwargs):
            shop = plata.shop_instance()
            order = shop.order_from_request(request)

            for check in checks:
                r = check(order=order, shop=shop, request=request)
                if r: return r

            return fn(request, order=order, *args, **kwargs)
        return wraps(fn)(_fn)
    return _dec


class Shop(object):
    """
    Plata's view and shop processing logic is contained inside this class.

    Shop needs a few model classes with relations between them:

    - Contact model linking to Django's auth.user
    - Order model with order items and an applied discount model
    - Discount model
    - Default currency for the shop (if you do not override default_currency
      in your own Shop subclass)

    Example::

        shop_instance = Shop(Contact, Order, Discount)

        urlpatterns = patterns('',
            url(r'^shop/', include(shop_instance.urls)),
        )
    """

    #: The base template used in all default checkout templates
    base_template = 'base.html'

    def __init__(self, contact_model, order_model, discount_model,
            default_currency=None, **kwargs):
        self.contact_model = contact_model
        self.order_model = order_model
        self.orderitem_model = self.order_model.items.related.model
        self.discount_model = discount_model
        self._default_currency = default_currency

        # Globally register the instance so that it can be accessed from
        # everywhere using plata.shop_instance()
        plata.register(self)

        for key, value in kwargs.items():
            if not hasattr(self, key):
                raise TypeError('%s() received an invalid keyword %r' % (
                    self.__class__.__name__, key))
            setattr(self, key, value)

    @property
    def urls(self):
        """Property offering access to the Shop-managed URL patterns"""
        return self.get_urls()

    def get_urls(self):
        return self.get_shop_urls() + self.get_payment_urls()

    def get_shop_urls(self):
        from django.conf.urls import patterns, url
        return patterns('',
            url(r'^cart/$', checkout_process_decorator(
                    order_already_confirmed, order_cart_warnings,
                )(self.cart),
                name='plata_shop_cart'),
            url(r'^checkout/$', checkout_process_decorator(
                cart_not_empty, order_already_confirmed, order_cart_validates,
                )(self.checkout),
                name='plata_shop_checkout'),
            url(r'^discounts/$', checkout_process_decorator(
                user_is_authenticated, cart_not_empty, order_already_confirmed, order_cart_validates,
                )(self.discounts),
                name='plata_shop_discounts'),
            url(r'^confirmation/$', checkout_process_decorator(
                    user_is_authenticated, cart_not_empty, order_cart_validates,
                )(self.confirmation),
                name='plata_shop_confirmation'),

            url(r'^order/success/$',
                self.order_success,
                name='plata_order_success'),
            url(r'^order/payment_failure/$',
                self.order_payment_failure,
                name='plata_order_payment_failure'),
            url(r'^order/new/$',
                self.order_new,
                name='plata_order_new'),
            )

    def get_payment_urls(self):
        from django.conf.urls import patterns, url, include
        urls = [url(r'', include(module.urls))
            for module in self.get_payment_modules()]
        return patterns('', *urls)

    def get_payment_modules(self, request=None):
        """
        Import and return all payment modules defined in
        ``PLATA_PAYMENT_MODULES``

        If request is given only applicable modules are loaded.
        """
        all_modules = [get_callable(module)(self)
            for module in plata.settings.PLATA_PAYMENT_MODULES]
        if not request:
            return all_modules
        return [module for module in all_modules
            if module.enabled_for_request(request)]
        
    def user_is_authenticated(self, user):
        """overwrite this for custom authentication check. This is needed to support lazysignup"""
        return (user and user.is_authenticated())
    
    def user_login(self, request, user):
        auth.login(request, user)

    def default_currency(self, request=None):
        """
        Return the default currency for instantiating new orders

        Override this with your own implementation if you have a
        multi-currency shop with auto-detection of currencies.
        """
        return self._default_currency or plata.settings.CURRENCIES[0]

    def set_order_on_request(self, request, order):
        """
        Helper method encapsulating the process of setting the current order
        in the session. Pass ``None`` if you want to remove any defined order
        from the session.
        """
        if order:
            request.session['shop_order'] = order.pk
        elif 'shop_order' in request.session:
            del request.session['shop_order']
            
    def create_order_for_user(self, user, request=None):
        """Creates and returns a new order for the given user."""
        contact = self.contact_from_user(user)
        #we can't check for user_is_authenticated, because in the lazy_user case, it might return false, even though the user is a persistet model
        order_user = None if isinstance(user, AnonymousUser) else user

        order = self.order_model.objects.create(
            currency=getattr(contact, 'currency',
                self.default_currency(request)),
            user=order_user,
            language_code=get_language(),
            )
        
        return order

    def order_from_request(self, request, create=False):
        """
        Instantiate the order instance for the current session. Optionally
        creates a new order instance if ``create=True``.

        Returns ``None`` if unable to find an offer.
        """
        try:
            order_pk = request.session.get('shop_order')
            if order_pk is None:
                #check if the current user has a open order
                if self.user_is_authenticated(request.user):
                    order = self.order_model.objects.filter(user=request.user, status__lt=self.order_model.PAID).latest()
                    if order is not None:
                        self.set_order_on_request(request, order)
                        return order
                    
                raise ValueError("no order in session")
            return self.order_model.objects.get(pk=order_pk)
        except AttributeError:
            # request has no session
            return None
        except (ValueError, self.order_model.DoesNotExist):
            if create:
                order = self.create_order_for_user(request.user)
                self.set_order_on_request(request, order)
                return order

        return None

    def contact_from_user(self, user):
        """
        Return the contact object bound to the current user if the user is
        authenticated. Returns ``None`` if no contact exists.
        """
        if not self.user_is_authenticated(user):
            return None

        try:
            return self.contact_model.objects.get(user=user)
        except self.contact_model.DoesNotExist:
            return None

    def get_context(self, request, context, **kwargs):
        """
        Helper method returning a context dict. Override this if you
        need additional context variables.
        """
        ctx = {
            'base_template': self.base_template,
            }
        ctx.update(context)
        ctx.update(kwargs)
        return ctx

    def render(self, request, template, context):
        """
        Helper which just passes everything on to ``django.shortcuts.render``
        """
        return render(request, template, context)

    def reverse_url(self, url_name, *args, **kwargs):
        """
        Hook for customizing the reverse function
        """
        return reverse(url_name, *args, **kwargs)

    def redirect(self, url_name, *args, **kwargs):
        """
        Hook for customizing the redirect function when used as application
        content
        """
        return HttpResponseRedirect(
            self.reverse_url(url_name, *args, **kwargs))

    def cart(self, request, order):
        """Shopping cart view"""

        if not order or not order.items.count():
            return self.render_cart_empty(request, {
                'progress': 'cart',
                })

        OrderItemFormset = inlineformset_factory(
            self.order_model,
            self.orderitem_model,
            form = getattr(self, 'form', ModelForm),
            extra=0,
            fields=('quantity',),
            )

        if request.method == 'POST':
            formset = OrderItemFormset(request.POST, instance=order)

            if formset.is_valid():
                changed = False

                # We cannot directly save the formset, because the additional
                # checks in modify_item must be performed.
                for form in formset.forms:
                    if not form.instance.product_id:
                        form.instance.delete()
                        messages.warning(request,
                            _('%(name)s has been removed from the inventory'
                            ' and from your cart as well.') % {
                                'name': form.instance.name,
                                })
                        changed = True

                    elif (formset.can_delete
                            and formset._should_delete_form(form)):
                        if order.is_confirmed():
                            raise ValidationError(_('Cannot modify order once'
                                ' it has been confirmed.'),
                                code='order_sealed')

                        form.instance.delete()
                        changed = True

                    elif form.has_changed():
                        order.modify_item(form.instance.product,
                            absolute=form.cleaned_data['quantity'],
                            recalculate=False,
                            item=form.instance,
                            )
                        changed = True

                if changed:
                    order.recalculate_total()
                    messages.success(request, _('The cart has been updated.'))

                if 'checkout' in request.POST:
                    return self.redirect('plata_shop_checkout')
                return HttpResponseRedirect('.')
        else:
            formset = OrderItemFormset(instance=order)

        return self.render_cart(request, {
            'order': order,
            'orderitemformset': formset,
            'progress': 'cart',
            })

    def render_cart_empty(self, request, context):
        """Renders a cart-is-empty page"""
        context.update({'empty': True})

        return self.render(request, 'plata/shop_cart.html',
            self.get_context(request, context))

    def render_cart(self, request, context):
        """Renders the shopping cart"""
        return self.render(request, 'plata/shop_cart.html',
            self.get_context(request, context))

    def checkout_form(self, request, order):
        """Returns the address form used in the first checkout step"""

        # Only import plata.contact if necessary and if this method isn't
        # overridden
        from plata.contact.forms import CheckoutForm
        return CheckoutForm
    
    def authentication_form(self, request, order):
        """Returns the authentication form used in the first checkout step"""
        from django.contrib.auth.forms import AuthenticationForm
        return AuthenticationForm

    def checkout(self, request, order):
        """Handles the first step of the checkout process"""
        if not self.user_is_authenticated(request.user):
            AuthenticationForm = self.authentication_form(request, order)
            if request.method == 'POST' and '_login' in request.POST:
                loginform = AuthenticationForm(data=request.POST, prefix='login')

                if loginform.is_valid():
                    user = loginform.get_user()
                    self.user_login(request, user)

                    order.user = user
                    order.save()

                    return HttpResponseRedirect('.')
            else:
                loginform = AuthenticationForm(prefix='login')
        else:
            loginform = None

        if order.status < order.CHECKOUT:
            order.update_status(order.CHECKOUT, 'Checkout process started')

        OrderForm = self.checkout_form(request, order)

        orderform_kwargs = {
            'prefix': 'order',
            'instance': order,
            'request': request,
            'shop': self,
            }

        if request.method == 'POST' and '_checkout' in request.POST:
            orderform = OrderForm(request.POST, **orderform_kwargs)

            if orderform.is_valid():
                orderform.save()
                return self.redirect('plata_shop_discounts')
        else:
            orderform = OrderForm(**orderform_kwargs)

        return self.render_checkout(request, {
            'order': order,
            'loginform': loginform,
            'orderform': orderform,
            'progress': 'checkout',
            })

    def render_checkout(self, request, context):
        """Renders the checkout page"""
        return self.render(request, 'plata/shop_checkout.html',
            self.get_context(request, context))

    def discounts_form(self, request, order):
        """Returns the discount form"""
        return shop_forms.DiscountForm

    def discounts(self, request, order):
        """Handles the discount code entry page"""
        DiscountForm = self.discounts_form(request, order)

        kwargs = {
            'order': order,
            'discount_model': self.discount_model,
            'request': request,
            'shop': self,
            }

        if request.method == 'POST':
            form = DiscountForm(request.POST, **kwargs)

            if form.is_valid():
                form.save()

                if 'proceed' in request.POST:
                    return self.redirect('plata_shop_confirmation')
                return HttpResponseRedirect('.')
        else:
            form = DiscountForm(**kwargs)

        order.recalculate_total()

        return self.render_discounts(request, {
            'order': order,
            'form': form,
            'progress': 'discounts',
            })

    def render_discounts(self, request, context):
        """Renders the discount code entry page"""
        return self.render(request, 'plata/shop_discounts.html',
            self.get_context(request, context))

    def confirmation_form(self, request, order):
        """Returns the confirmation and payment module selection form"""
        return shop_forms.ConfirmationForm

    def confirmation(self, request, order):
        """
        Handles the order confirmation and payment module selection checkout
        step

        Hands off processing to the selected payment module if confirmation
        was successful.
        """
        order.recalculate_total()

        ConfirmationForm = self.confirmation_form(request, order)

        kwargs = {
            'order': order,
            'request': request,
            'shop': self,
            }

        if request.method == 'POST':
            form = ConfirmationForm(request.POST, **kwargs)

            if form.is_valid():
                return form.process_confirmation()
        else:
            form = ConfirmationForm(**kwargs)

        return self.render_confirmation(request, {
            'order': order,
            'form': form,
            # Whether the order had already been confirmed.
            'confirmed': request.GET.get('confirmed', False),
            'progress': 'confirmation',
            })

    def render_confirmation(self, request, context):
        """Renders the confirmation page"""
        return self.render(request, 'plata/shop_confirmation.html',
            self.get_context(request, context))

    def order_success(self, request):
        """
        Handles order successes (e.g. when an order has been successfully
        paid for)
        """
        order = self.order_from_request(request)

        if not order:
            return self.order_new(request)

        if not order.balance_remaining:
            # Create a new, empty order right away. It makes no sense
            # to keep the completed order around anymore.
            self.set_order_on_request(request, order=None)

        return self.render(request, 'plata/shop_order_success.html',
            self.get_context(request, {
                'order': order,
                'progress': 'success',
                }))

    def order_payment_failure(self, request):
        """Handles order payment failures"""
        order = self.order_from_request(request)

        logger.warn('Order payment failure for %s' % order.order_id)

        if plata.settings.PLATA_STOCK_TRACKING:
            StockTransaction = plata.stock_model()

            for transaction in order.stock_transactions.filter(
                    type=StockTransaction.PAYMENT_PROCESS_RESERVATION):
                transaction.delete()

        order.payments.pending().delete()

        if order.payments.authorized().exists():
            # There authorized order payments around!
            messages.warning(request, _('Payment failed, please try again.'))
            logger.warn('Order %s is already partially paid, but payment'
                ' failed anyway!' % order.order_id)
        elif order.status > order.CHECKOUT and order.status < order.PAID:
            order.update_status(order.CHECKOUT,
                'Order payment failure, going back to checkout')
            messages.info(request,
                _('Payment failed; you can continue editing your order and'
                    ' try again.'))

        return self.render(request, 'plata/shop_order_payment_failure.html',
            self.get_context(request, {
                'order': order,
                'progress': 'failure',
                }))

    def order_new(self, request):
        """
        Forcibly create a new order and redirect user either to the frontpage
        or to the URL passed as ``next`` GET parameter
        """
        self.set_order_on_request(request, order=None)

        next = request.GET.get('next')
        if next:
            return HttpResponseRedirect(next)

        return HttpResponseRedirect('/')
