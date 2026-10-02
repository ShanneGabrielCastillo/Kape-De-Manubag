# Profile Image Filename Display — Change Report

## 1. What the previously displayed value represented

The template used `{{ user.profile_image.name|cut:"profiles/" }}`.
`profile_image.name` is Django's `ImageField.name` — the storage path relative
to `MEDIA_ROOT` (e.g. `profiles/a9f2578e-f03a-4ccd-97e6-07fe6d6c5148-1_all_8909_yfqve2`).
After the `cut:"profiles/"` filter removed the prefix, the user saw the raw
Cloudinary-generated public ID portion, for example:

```
a9f2578e-f03a-4ccd-97e6-07fe6d6c5148-1_all_8909_yfqve2
```

This is a Cloudinary storage identifier, not the original filename the user chose.

## 2. Where it came from

Cloudinary's Django storage backend replaces the original filename with a UUID /
public-ID string when the image is uploaded. Django stores that generated name as
`profile_image.name` (the path in storage). There was no separate field
recording the original filename the user selected before upload.

## 3. How the user-friendly filename is now obtained / stored

A new `profile_image_filename` `CharField` (max 255 chars, blank allowed,
default `''`) was added to `CustomUser`. `ProfileUpdateForm.save()` now reads
`cleaned_data['profile_image']` and, when it is an `UploadedFile` (i.e. a new
file was chosen by the user), extracts `os.path.basename(file.name)` — the
name the browser sent, e.g. `picture.jpg` — and stores it in
`user.profile_image_filename`.

The template then displays `user.profile_image_filename` when it is non-empty,
or falls back to `Profile photo` for images already in the database that have
no recorded original filename.

## 4. Files changed

| File | Change |
|---|---|
| `apps/accounts/models.py` | Added `profile_image_filename = CharField(max_length=255, blank=True, default='')` after `profile_image`. |
| `apps/accounts/migrations/0004_customuser_profile_image_filename.py` | Auto-generated migration that adds the column. |
| `apps/accounts/forms.py` | Added `save()` override to `ProfileUpdateForm` that captures `os.path.basename(new_image.name)` into `user.profile_image_filename` when a new image is uploaded. |
| `apps/accounts/views.py` | Remove Photo branch now also sets `user.profile_image_filename = ''` and includes the field in `update_fields`. |
| `templates/accounts/profile.html` | (a) `.profile-img-filename` CSS changed from `word-break: break-all` to `overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: block; max-width: 100%`. (b) Filename `<p>` changed to display `user.profile_image_filename` (with `Profile photo` fallback) instead of the raw storage path. |

## 5. Database / schema changes

**Yes.** One new column was added:

```sql
ALTER TABLE accounts_customuser ADD COLUMN profile_image_filename VARCHAR(255) NOT NULL DEFAULT '';
```

Applied via migration `0004_customuser_profile_image_filename`. No existing
rows were modified; the column defaults to `''`.

## 6. Cloudinary / storage not disrupted

Confirmed. The `profile_image` `ImageField`, its `upload_to`, Cloudinary
credentials, the upload/validation pipeline, the 5 MB limit, and supported
formats are all unchanged. The new field is purely a display aid stored
separately in the application database.

## 7. Existing-image behaviour

Existing users whose `profile_image` was uploaded before this change have
`profile_image_filename = ''` (the column default). The template detects the
empty string and displays `Profile photo` instead of the Cloudinary ID.
The image itself continues to load normally from the same Cloudinary URL.

## 8. New-upload behaviour

When a user selects a file (e.g. `picture.jpg`) and saves the profile,
`ProfileUpdateForm.save()` captures `picture.jpg` and stores it in
`profile_image_filename`. The template then displays `picture.jpg` under
the avatar.

## 9. Remove Photo behaviour

Clicking Remove Photo triggers the existing `remove_photo=1` POST path.
The view now additionally sets `profile_image_filename = ''` and saves both
`profile_image` and `profile_image_filename` in `update_fields`. After removal
the filename element is empty (no image → no text shown), matching the
expected UX.

## 10. CSS / overflow handling

`.profile-img-filename` now uses:

```css
max-width: 100%;
overflow: hidden;
text-overflow: ellipsis;
white-space: nowrap;
display: block;
```

Very long filenames are truncated with `…` rather than causing horizontal
overflow. The browser's native tooltip (via the title attribute, if added in
future) would reveal the full name; the current implementation keeps the layout
clean across all tested viewport widths (320 px – desktop).
