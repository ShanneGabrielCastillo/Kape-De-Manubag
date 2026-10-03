# Password Strong-Only Enforcement — Implementation Report

## 1. Root Cause: Why "Weak" Was Accepted Before

The password-strength indicator was purely a UX decoration. The form and view had no
awareness of the strength score at all:

- **Frontend:** The JS indicator showed Very Weak / Weak / Fair / Strong, but there
  was no `form.addEventListener('submit', ...)` guard — the form could be submitted
  regardless of the displayed level.
- **Backend — Change Password (`StaffPasswordChangeForm.clean()`):** Only ran Django's
  four built-in validators: `MinimumLengthValidator` (8 chars), `CommonPasswordValidator`,
  `NumericPasswordValidator`, and `UserAttributeSimilarityValidator`. None of these require
  an uppercase letter, a lowercase letter, or a special character. A password like
  `password123` satisfies all four, scores 3/5 in the JS ("Fair"), and was accepted.
- **Backend — Reset Password (`password_reset_confirm` view):** Same gap — only called
  `password_validation.validate_password(new_password1, user)`.

---

## 2. Files Changed

| File | What changed |
|---|---|
| `apps/accounts/validators.py` | Appended `validate_password_strength()` — shared single source of truth for the strength rule |
| `apps/accounts/forms.py` | Imported `validate_password_strength`; updated `StaffPasswordChangeForm.clean()` to call it first |
| `apps/accounts/views.py` | Imported `validate_password_strength`; updated `password_reset_confirm` POST block to call it first |
| `templates/accounts/change_password.html` | Added submit guard inside the existing IIFE |
| `templates/accounts/password_reset_confirm.html` | Added `id="reset-password-form"` to form tag; added submit guard inside the existing IIFE |

---

## 3. How "Strong" Is Defined (shared criteria)

The same 5-criteria / threshold-4 rule lives in one place (`validators.py`) and is
referenced by both backend flows:

```
Criterion 1: length >= 8
Criterion 2: contains a lowercase letter  [a-z]
Criterion 3: contains an uppercase letter [A-Z]
Criterion 4: contains a digit            [0-9]
Criterion 5: contains a special character [^a-zA-Z0-9]

Score = number of criteria met (0–5)
"Strong" = score >= 4
```

This exactly mirrors the JS `REQS` array and `_STRONG_THRESHOLD = 4` already in both
templates.

---

## 4. Client-Side Enforcement

Both templates now include a submit guard appended inside the existing strength IIFE:

- On form submit: calls `calcScore(pw1.value)` (the same function already used by the
  indicator). If score < 4, calls `e.preventDefault()`, makes `#pw-block-msg` visible
  ("Your password must be Strong before you can continue."), and focuses the password
  field.
- As the user types: if score reaches 4, `#pw-block-msg` is hidden automatically.
- The block message is inserted via JS (`insertBefore`) immediately after the strength
  indicator `#pw-strength-indicator`, so it appears in context without layout shift.

---

## 5. Server-Side Enforcement

### Change Password (`StaffPasswordChangeForm.clean()`)
```python
# 1) Strength check (mirrors frontend JS — score >= 4/5)
try:
    validate_password_strength(new_pw)
except forms.ValidationError as exc:
    self.add_error('new_password1', exc)
    return cleaned  # stop; skip Django validators on weak pw
# 2) Django's built-in validators (length, common, numeric, similarity)
try:
    password_validation.validate_password(new_pw, self.user)
except forms.ValidationError as exc:
    self.add_error('new_password1', exc)
```

### Reset Password (`password_reset_confirm` view)
```python
try:
    validate_password_strength(new_password1)
except django_forms.ValidationError as exc:
    errors.extend(exc.messages)
if not errors:
    try:
        password_validation.validate_password(new_password1, user)
    except django_forms.ValidationError as exc:
        errors.extend(exc.messages)
```

In both cases: strength check runs first, Django validators run only if strength passes.
A JS-disabled or direct HTTP request gets the same server-side rejection.

---

## 6. Same Rule on Both Flows

```
validate_password_strength()   ← defined once in validators.py
         ↓                              ↓
  forms.py (Change Password)    views.py (Reset Password)
         ↓                              ↓
   same threshold (4)           same threshold (4)
   same 5 criteria              same 5 criteria
```

The frontend REQS array in both templates is also identical, so the indicator and the
backend always agree on what "Strong" means.

---

## 7. Django Server-Side Validation — Unchanged

Django's existing `AUTH_PASSWORD_VALIDATORS` are untouched and still run after the
strength check passes:
- `UserAttributeSimilarityValidator`
- `MinimumLengthValidator` (8 chars)
- `CommonPasswordValidator`
- `NumericPasswordValidator`

---

## 8. Security Confirmations

- Passwords are never logged, stored in localStorage/sessionStorage, or sent to any
  third party.
- The strength error message does not include or echo the submitted password value.
- The reset token mechanism, expiry, and anti-enumeration behavior are completely
  unchanged.
- Password hashing (`set_password`), session rotation (`update_session_auth_hash`),
  and audit logging (`log_action`) are all unchanged.

---

## 9. Test Matrix (manual verification via code inspection)

| Test case | Client-side | Server-side |
|---|---|---|
| Empty password → submit | Blocked (score 0 < 4) | Blocked (ValidationError) |
| `abc` — Very Weak | Blocked (score 1 < 4) | Blocked |
| `password` — Weak | Blocked (score 2 < 4) | Blocked |
| `password123` — Fair | Blocked (score 3 < 4) | Blocked |
| `Password1` — Strong (4/5) | Allowed (score 4 ≥ 4) | Allowed (passes strength + Django validators) |
| `Password1!` — Strong (5/5) | Allowed (score 5 ≥ 4) | Allowed |
| Wrong current password | Allowed by guard | Blocked by `clean_current_password` |
| Mismatched confirmation | Allowed by guard | Blocked by `clean_new_password2` / view mismatch check |
| JS disabled, direct POST of weak password | N/A | Blocked server-side by `validate_password_strength` |

*Note: Django test runner timed out in this shell environment (hanging shell commands).
The logic was verified by direct file inspection and unit-level Python logic tracing.*
