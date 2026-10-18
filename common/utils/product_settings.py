"""Settings supported by this product edition."""
EXCLUDED_PREFIXES = ('ad_price.', 'auth.footer_ad', 'distribution.', 'alipay.', 'withdraw.', 'promotion.', 'activation.', 'user.renew_month_price')
EXCLUDED_KEYS = {'balance', 'payment_qrcode', 'card_secret_key', 'dock_code'}
def is_removed_setting(key: str) -> bool:
    return key in EXCLUDED_KEYS or key.startswith(EXCLUDED_PREFIXES)
