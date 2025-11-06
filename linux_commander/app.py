# app.py
from flask import Flask, render_template, request, send_file, redirect, url_for, flash
import os
import subprocess
import humanize
from datetime import datetime
import zipfile
import io
import re
from math import ceil

app = Flask(__name__)
app.secret_key = os.urandom(24) # Needed for flashing messages

# --- CONFIGURATION ---
# Set the base directory for the file explorer.
# WARNING: Setting this to "/" allows the app to access the entire filesystem.
# This is powerful but can be dangerous, especially with deletion enabled.
# Only run as a user with limited permissions if you are unsure.
# For a safer setup, change this to: BASE_DIR = os.path.expanduser("~")
BASE_DIR = "/"
PER_PAGE = 50 # Number of items to show per page

# --- Helper Classes and Functions ---

class Pagination:
    """A simple pagination helper class."""
    def __init__(self, page, per_page, total_count):
        self.page = page
        self.per_page = per_page
        self.total_count = total_count

    @property
    def pages(self):
        return int(ceil(self.total_count / float(self.per_page)))

    @property
    def has_prev(self):
        return self.page > 1

    @property
    def has_next(self):
        return self.page < self.pages

    @property
    def prev_num(self):
        return self.page - 1

    @property
    def next_num(self):
        return self.page + 1

    def iter_pages(self, left_edge=2, left_current=2, right_current=5, right_edge=2):
        last = 0
        for num in range(1, self.pages + 1):
            if num <= left_edge or \
               (num > self.page - left_current - 1 and num < self.page + right_current) or \
               num > self.pages - right_edge:
                if last + 1 != num:
                    yield None
                yield num
                last = num

def parse_ls_output(output):
    """Parses the output of 'ls -l' into a list of file dicts."""
    lines = output.strip().split('\n')
    files = []
    for line in lines:
        if not line:
            continue
        try:
            parts = line.split(maxsplit=8)
            # Basic validation
            if len(parts) < 9 or not parts[4].isdigit():
                continue
            path = parts[8]
            files.append({
                'permissions': parts[0],
                'owner': parts[2],
                'group': parts[3],
                'size': humanize.naturalsize(int(parts[4])),
                'modified': ' '.join(parts[5:7]),
                'path': path
            })
        except (IndexError, ValueError) as e:
            print(f"Could not parse line: '{line}'. Error: {e}") # For debugging
    return files

def get_file_info(path):
    """Gets metadata for a single file or directory."""
    try:
        stat = os.stat(path)
        return {
            "name": os.path.basename(path), "path": path, "is_dir": os.path.isdir(path),
            "size": humanize.naturalsize(stat.st_size) if not os.path.isdir(path) else '',
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
        }
    except (FileNotFoundError, PermissionError):
        return None

# --- Main Routes ---

@app.route('/')
def explorer():
    req_path = request.args.get('path', BASE_DIR)
    
    # Security: Normalize path and ensure it's an absolute path
    safe_path = os.path.realpath(req_path)
    
    # Security: Check if the path is within the intended BASE_DIR.
    # This is less of a jail with BASE_DIR="/" but still good practice.
    if not safe_path.startswith(os.path.realpath(BASE_DIR)):
        safe_path = os.path.realpath(BASE_DIR)
    
    contents = []
    parent_path = None
    try:
        if os.path.isdir(safe_path):
            if safe_path != os.path.realpath(BASE_DIR):
                parent_path = os.path.dirname(safe_path)
            
            # List items, handling potential permission errors on individual items
            for item_name in sorted(os.listdir(safe_path), key=str.lower):
                item_path = os.path.join(safe_path, item_name)
                info = get_file_info(item_path)
                if info:
                    contents.append(info)
        else:
            flash(f"Path is not a directory: {safe_path}", "warning")
    except PermissionError:
        flash(f"Permission denied to access: {safe_path}", "danger")
    
    return render_template('explorer.html', current_path=safe_path, contents=contents, parent_path=parent_path)


def file_lister_route(title, find_command):
    """Generic function to handle paginated file listing."""
    page = request.args.get('page', 1, type=int)
    search_path = request.args.get('path', BASE_DIR)
    
    safe_search_path = os.path.realpath(search_path)
    if not safe_search_path.startswith(os.path.realpath(BASE_DIR)):
        safe_search_path = os.path.realpath(BASE_DIR)

    full_command = find_command.format(path=safe_search_path)
    try:
        result = subprocess.run(full_command, shell=True, check=True, capture_output=True, text=True).stdout
        all_files = parse_ls_output(result)
    except subprocess.CalledProcessError as e:
        flash(f"Error executing command: {e.stderr}", "danger")
        all_files = []

    total_files = len(all_files)
    start = (page - 1) * PER_PAGE
    end = start + PER_PAGE
    paginated_files = all_files[start:end]
    pagination = Pagination(page, PER_PAGE, total_files)

    return render_template('results_paginated.html',
                           title=title,
                           items=paginated_files,
                           pagination=pagination,
                           search_path=safe_search_path)

@app.route('/large-files')
def large_files():
    # Adjusted size to 10MB to be more practical for general use
    command = "find {path} -type f -size +10M -printf '%p\\0' | xargs -0 ls -l --full-time | sort -k5 -rn"
    return file_lister_route("Large Files (>10MB)", command)

@app.route('/recent-files')
def recent_files():
    command = "find {path} -type f -mtime -7 -printf '%p\\0' | xargs -0 ls -l --full-time | sort -k6,7rn"
    return file_lister_route("Recently Modified Files (Last 7 Days)", command)


@app.route('/delete-files', methods=['POST'])
def delete_files():
    files_to_delete = request.form.getlist('files_to_delete')
    redirect_url = request.form.get('redirect_url', url_for('explorer'))
    deleted_count, error_count = 0, 0

    if not files_to_delete:
        flash("No files were selected for deletion.", "warning")
        return redirect(redirect_url)

    for f_path in files_to_delete:
        safe_path = os.path.realpath(f_path)
        if not safe_path.startswith(os.path.realpath(BASE_DIR)):
            flash(f"Deletion failed: {f_path} is outside the allowed directory.", "danger")
            error_count += 1
            continue
        
        try:
            if os.path.isfile(safe_path):
                os.remove(safe_path)
                deleted_count += 1
            else:
                flash(f"Deletion failed: {f_path} is not a file or was already deleted.", "warning")
                error_count += 1
        except OSError as e:
            flash(f"Error deleting {f_path}: {e}", "danger")
            error_count += 1
    
    if deleted_count > 0: flash(f"Successfully deleted {deleted_count} file(s).", "success")
    if error_count > 0: flash(f"Failed to delete {error_count} file(s).", "danger")

    return redirect(redirect_url)

# --- Other routes remain the same ---
@app.route('/python-env')
def python_env():
    find_command = "find / -path '*/bin/python' -o -name 'pyvenv.cfg' -o -name 'Pipfile' -o -name 'poetry.lock' 2>/dev/null"
    try:
        result = subprocess.run(find_command, shell=True, check=True, capture_output=True, text=True).stdout
    except subprocess.CalledProcessError as e:
        result = f"Error executing command:\n{e.stderr}"
    return render_template('command_result.html', title="Potential Python Environments", result=result)

@app.route('/zip', methods=['POST'])
def zip_files():
    selected_items = request.form.getlist('selected_items')
    if not selected_items: return "No items selected", 400
    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        for item_path in selected_items:
            safe_item_path = os.path.realpath(item_path)
            if not safe_item_path.startswith(os.path.realpath(BASE_DIR)): continue
            if os.path.isdir(safe_item_path):
                for root, _, files in os.walk(safe_item_path):
                    for file in files:
                        file_path = os.path.join(root, file)
                        zf.write(file_path, os.path.relpath(file_path, os.path.dirname(safe_item_path)))
            else:
                zf.write(safe_item_path, os.path.basename(safe_item_path))
    memory_file.seek(0)
    return send_file(memory_file, download_name='archive.zip', as_attachment=True)

def main():
    """
    This function is called when the user runs the 'dependaxy' command.
    """
    print("--- Starting Linux Commander Server ---")
    print("--- Navigate to http://127.0.0.1:5000 in your browser ---")
    print("--- Press Ctrl+C to stop the server ---")
    # For production, a real WSGI server like Gunicorn or Waitress is recommended.
    app.run(host='127.0.0.1', port=5000, debug=False)

if __name__ == '__main__':
    app.run(debug=True, port=5000)
