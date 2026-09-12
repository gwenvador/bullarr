from pathlib import Path

def test_rename_existing_directory_moves_files_instead_of_failing():
    source = Path("/home/gwen/docker/bullarr-wt-rename-existing/blueprints/library/routes.py").read_text()
    assert "destination_dir_exists" in source
    assert "shutil.move(source_file, destination_file)" in source
    assert "Fichier destination existe déjà" in source
