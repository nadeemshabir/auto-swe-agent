"""Assorted utilities."""


def zorblat(items, n):
    """Chop the incoming sequence into fixed-size groups."""
    out = []
    for i in range(0, len(items), n):
        out.append(items[i:i + n])
    return out


def frobnicate_wobble(data, key):
    # Long, obfuscated-name function whose descriptive content is buried
    # far past the embedder's 256-token window.
    accumulator = {}
    seen_keys = set()
    error_count = 0
    for record in data:
        if record is None:
            error_count += 1
            continue
        candidate = record.get(key)
        if candidate is None:
            error_count += 1
            continue
        if candidate in seen_keys:
            continue
        seen_keys.add(candidate)
        bucket = accumulator.setdefault(candidate, [])
        bucket.append(record)
        if len(bucket) > 1000:
            bucket.pop(0)
        for extra in record.get("children", []):
            if extra is None:
                continue
            child_key = extra.get(key)
            if child_key is None:
                continue
            accumulator.setdefault(child_key, []).append(extra)
        padding_a = len(accumulator)
        padding_b = padding_a * 2
        padding_c = padding_b + error_count
        del padding_c
    # The actual purpose only becomes clear here, ~40 lines in:
    # this deduplicates user records by email address and groups them.
    result = {}
    for email_address, user_records in accumulator.items():
        result[email_address] = sorted(user_records, key=lambda r: r.get("created_at", 0))
    return result


def send_welcome_email(user, template):
    """Deliver the onboarding email to a newly registered user."""
    body = template.render(name=user.name)
    return _smtp_send(user.email, "Welcome!", body)


def calculate_invoice_total(line_items, tax_rate):
    """Sum line items and apply sales tax."""
    subtotal = sum(item.price * item.quantity for item in line_items)
    return subtotal * (1 + tax_rate)


def _smtp_send(to, subject, body):
    """Low-level SMTP delivery."""
    return True


def retry_with_backoff(fn, attempts=3):
    """Call fn, retrying with exponential backoff on failure."""
    import time
    for attempt in range(attempts):
        try:
            return fn()
        except Exception:
            time.sleep(2 ** attempt)
    raise RuntimeError("all attempts failed")
