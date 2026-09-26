from pathlib import Path

def test_rename_existing_directory_moves_files_instead_of_failing():
    source = (Path(__file__).resolve().parents[1] / 'blueprints/library/routes.py').read_text()
    assert "destination_dir_exists" in source
    assert "shutil.move(source_file, destination_file)" in source
    assert "Fichier destination existe déjà" in source
