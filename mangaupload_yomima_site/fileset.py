import zipfile
import os
import shutil

# Define the name of the output zip file
zip_file_name = 'archive.zip'

# List of directories and files to include in the zip
items_to_zip = [
    'yomima',  # Directory created earlier
    'requirements.txt', # File created earlier
    'main.py', # File moved earlier
    'setup_app.py'  # File created earlier
]

# Add 'static' and 'storage' if they exist at the root level
# Note: The context does not show 'static' or 'storage' directories directly at the root,
# but rather '/content/yomima/static/style.css'.
# If you meant the 'static' inside 'yomima', adjust the path.
# If you intend to create them and then zip, please clarify.
if os.path.exists('static'):
    items_to_zip.append('static')
if os.path.exists('storage'):
    items_to_zip.append('storage')

# Ensure target directories exist before moving (idempotent)
os.makedirs('yomima/static', exist_ok=True)
os.makedirs('yomima/templates', exist_ok=True)

# Move style.css from root to yomima/static/ if it exists in root
if os.path.exists('style.css'):
    shutil.move('style.css', 'yomima/static/style.css')
    print("Moved style.css from root to yomima/static/")

# Move reader.html from root to yomima/templates/ if it exists in root
if os.path.exists('reader.html'):
    shutil.move('reader.html', 'yomima/templates/reader.html')
    print("Moved reader.html from root to yomima/templates/")


with zipfile.ZipFile(zip_file_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
    for item in items_to_zip:
        if os.path.isdir(item):
            for root, _, files in os.walk(item):
                for file in files:
                    file_path = os.path.join(root, file)
                    zipf.write(file_path, os.path.relpath(file_path, os.path.dirname(item)))
            print(f"Added directory: {item}/")
        elif os.path.isfile(item):
            zipf.write(item)
            print(f"Added file: {item}")
        else:
            print(f"Warning: {item} not found, skipping.")

print(f'Successfully created {zip_file_name}')