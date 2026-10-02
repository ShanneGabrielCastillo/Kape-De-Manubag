# Password Strength Indicator — Implementation Report

## Django Password Validators Found

From `kape_de_manubag/settings.py` (`AUTH_PASSWORD_VALIDATORS`):

| Validator | Effect |
|---|---|
| `UserAttributeSimilarityValidator` | Rejects passwords too similar to username/email/first name/last name |
| `MinimumLengthValidator` | Requires at least 8 characters (Django default) |
| `CommonPasswordValidator` | Rejects passwords on the common-password list |
| `NumericPasswordValidator` | Rejects entirely numeric passwords |

No custom validators were found. The minimum length is Django's default of **8 characters**.

## Strength Levels Implemented

| Level | Criteria |
|---|---|
| **Enter a password** (neutral) | Field is empty |
| **Very Weak** | 1 of 5 requirements met |
| **Weak** | 2 of 5 requirements met |
| **Fair** | 3 of 5 requirements met |
| **Strong** | 4 or 5 of 5 requirements met |

## Requirements Checked (client-side, display only)

1. At least 8 characters
2. Lowercase letter (a–z)
3. Uppercase letter (A–Z)
4. Contains a number
5. Special character (any non-alphanumeric)

These are approximate indicators that align with good password hygiene. Django's `CommonPasswordValidator` and `UserAttributeSimilarityValidator` cannot be replicated client-side; the frontend indicator is a UX aid only.

## Files Changed

1. `templates/accounts/change_password.html` — added `#pw-strength-indicator` div after the `id_new_password1` wrapper, added `#pw-match-indicator` div after the `id_new_password2` wrapper, expanded the single `<script>` block to include strength + match logic.
2. `static/css/main.css` — appended the `/* -- Password Strength Indicator -- */` block at the end of the file.

## Security Confirmations

- **Django server-side validation remains the final authority.** The frontend indicator is purely cosmetic; the form still submits to Django, which runs all configured `AUTH_PASSWORD_VALIDATORS` before accepting a password change.
- **No passwords are stored.** The strength calculation is performed in an immediately-invoked function expression (IIFE). The password value is never written to `localStorage`, `sessionStorage`, cookies, or any other persistent store.
- **No passwords are logged.** No `console.log` or similar calls reference the password value.
- **No passwords are sent to third parties.** There are no external API calls. All strength calculations happen locally in the browser using pure JavaScript string operations.
- **Existing submit behavior is preserved.** The submit button is not disabled by the strength indicator; Django enforces final acceptance or rejection.
