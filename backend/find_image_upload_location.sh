#!/bin/bash
# find_image_upload_location.sh
# Run from your backend repo root

echo "=== Searching for image upload / storage code ==="
echo ""

echo "--- Looking for common storage keywords ---"
grep -rniE "s3|boto3|cloudinary|gcs|google.cloud.storage|upload_file|UPLOAD_DIR|MEDIA_ROOT|storage_path" \
  --include="*.py" . 2>/dev/null | grep -v "node_modules\|.venv\|__pycache__" | head -50

echo ""
echo "--- Looking for .env / config for storage settings ---"
grep -niE "BUCKET|STORAGE|S3_|CLOUDINARY|MEDIA_URL|UPLOAD" .env* config*.py settings*.py 2>/dev/null

echo ""
echo "--- Looking for existing product image URL patterns in DB models ---"
grep -rniE "image_url|photo_url" --include="*.py" . 2>/dev/null | grep -v "node_modules\|.venv" | head -20

echo ""
echo "=== Done. Check above for bucket name / upload function / folder convention ==="
