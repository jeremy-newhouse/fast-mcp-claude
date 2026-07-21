"""Tests for the file-bridge tools (list_files/read_file/write_file) and the
FMC-11 TOCTOU-race defense in utils/secure_fs.py.

The TOCTOU tests simulate the real attack window -- a co-located process
swapping a path component for a symlink pointing outside WORKSPACE_ROOTS --
deterministically and single-threaded, by injecting the filesystem mutation
exactly between validate_workspace_path's containment check (the "time of
check") and the tool's real filesystem operation (the "time of use").
"""

import pytest

from fast_mcp_claude.tools import files as files_mod
from fast_mcp_claude.utils.secure_fs import secure_open_read, secure_open_write, secure_scandir


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET OUTSIDE SANDBOX")
    monkeypatch.setattr(files_mod.settings, "workspace_roots", str(root))
    return root, outside


def _swap_after_validate(monkeypatch, swap):
    """Hook the exact TOCTOU window: run the real validate_workspace_path
    (the check), apply `swap` to the filesystem, then hand the already-resolved
    Path back to the tool -- which then performs its real operation (the use)
    against a filesystem that changed out from under it."""
    real = files_mod.validate_workspace_path

    def wrapper(*args, **kwargs):
        resolved = real(*args, **kwargs)
        swap(resolved)
        return resolved

    monkeypatch.setattr(files_mod, "validate_workspace_path", wrapper)


class TestSecureFsPrimitives:
    """Direct unit tests of the race-safe primitives themselves."""

    def test_scandir_rejects_directory_swapped_for_symlink(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        victim = root / "victim"
        victim.mkdir()

        resolved = victim.resolve()  # simulates validate_workspace_path's return
        victim.rmdir()
        victim.symlink_to(outside)  # the race: swap AFTER "validation"

        with pytest.raises(NotADirectoryError):
            with secure_scandir(resolved, [root.resolve()]):
                pass

    def test_scandir_rejects_ancestor_swapped_for_symlink(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "a" / "b").mkdir(parents=True)

        resolved = (root / "a" / "b").resolve()
        (root / "a" / "b").rmdir()
        (root / "a").rmdir()
        (root / "a").symlink_to(outside)  # swap an ANCESTOR, not the leaf

        with pytest.raises(NotADirectoryError):
            with secure_scandir(resolved, [root.resolve()]):
                pass

    def test_open_read_rejects_file_swapped_for_symlink(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("SECRET")
        victim = root / "victim.txt"
        victim.write_text("legit")

        resolved = victim.resolve()
        victim.unlink()
        victim.symlink_to(outside / "secret.txt")

        with pytest.raises(OSError) as exc_info:
            with secure_open_read(resolved, [root.resolve()]):
                pass
        assert exc_info.value.errno == 62 or "symbolic link" in str(exc_info.value).lower()

    def test_open_write_rejects_file_swapped_for_symlink(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        victim = root / "victim.txt"
        victim.write_text("legit")

        resolved = victim.resolve()
        victim.unlink()
        victim.symlink_to(outside / "planted.txt")

        with pytest.raises(OSError):
            with secure_open_write(resolved, [root.resolve()], overwrite=True):
                pass
        assert not (outside / "planted.txt").exists()

    def test_open_write_rejects_raced_parent_mkdir(self, tmp_path):
        """AC#3: write_file's parent-directory creation is itself a second
        traversal that can be raced -- simulate another process winning the
        mkdir race by planting a symlink at the parent path first."""
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()

        resolved = (root / "newdir").resolve() / "file.txt"  # parent doesn't exist yet
        (root / "newdir").symlink_to(outside)  # attacker wins the mkdir race

        with pytest.raises(OSError):
            with secure_open_write(resolved, [root.resolve()], overwrite=True):
                pass
        assert list(outside.iterdir()) == []

    def test_open_write_overwrite_false_is_atomic(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        existing = root / "existing.txt"
        existing.write_text("already here")

        with pytest.raises(FileExistsError):
            with secure_open_write(existing.resolve(), [root.resolve()], overwrite=False):
                pass


class TestListFilesToctou:
    async def test_swapped_target_dir_fails_closed(self, sandbox, monkeypatch):
        root, outside = sandbox
        victim = root / "victim"
        victim.mkdir()

        def swap(resolved):
            victim.rmdir()
            victim.symlink_to(outside)

        _swap_after_validate(monkeypatch, swap)
        result = await files_mod.list_files(path=str(victim))

        assert result["success"] is False
        assert result["error"]["code"] == "PERMISSION_DENIED"
        assert "secret.txt" not in str(result)

    async def test_normal_listing_still_works(self, sandbox):
        root, _ = sandbox
        (root / "a.txt").write_text("hi")
        (root / "sub").mkdir()

        result = await files_mod.list_files(path=str(root))

        assert result["success"] is True
        names = {e["name"] for e in result["entries"]}
        assert names == {"a.txt", "sub"}


class TestReadFileToctou:
    async def test_swapped_target_file_fails_closed(self, sandbox, monkeypatch):
        root, outside = sandbox
        victim = root / "victim.txt"
        victim.write_text("legit content")

        def swap(resolved):
            victim.unlink()
            victim.symlink_to(outside / "secret.txt")

        _swap_after_validate(monkeypatch, swap)
        result = await files_mod.read_file(path=str(victim))

        assert result["success"] is False
        assert result["error"]["code"] == "PERMISSION_DENIED"
        assert "SECRET" not in str(result)

    async def test_normal_read_still_works(self, sandbox):
        root, _ = sandbox
        f = root / "hello.txt"
        f.write_text("hello world")

        result = await files_mod.read_file(path=str(f))

        assert result["success"] is True
        assert result["content"] == "hello world"


class TestWriteFileToctou:
    async def test_swapped_target_file_fails_closed(self, sandbox, monkeypatch):
        root, outside = sandbox
        victim = root / "victim.txt"
        victim.write_text("legit")

        def swap(resolved):
            victim.unlink()
            victim.symlink_to(outside / "planted.txt")

        _swap_after_validate(monkeypatch, swap)
        result = await files_mod.write_file(path=str(victim), content="pwned")

        assert result["success"] is False
        assert result["error"]["code"] == "PERMISSION_DENIED"
        assert not (outside / "planted.txt").exists()

    async def test_swapped_parent_dir_fails_closed(self, sandbox, monkeypatch):
        """AC#3: a to-be-created parent directory swapped for a symlink."""
        root, outside = sandbox
        target = root / "newdir" / "file.txt"

        def swap(resolved):
            (root / "newdir").symlink_to(outside)

        _swap_after_validate(monkeypatch, swap)
        result = await files_mod.write_file(path=str(target), content="pwned")

        assert result["success"] is False
        assert result["error"]["code"] == "PERMISSION_DENIED"
        assert list(outside.iterdir()) == [outside / "secret.txt"]

    async def test_normal_write_still_works(self, sandbox):
        root, _ = sandbox
        target = root / "sub" / "new.txt"

        result = await files_mod.write_file(path=str(target), content="written")

        assert result["success"] is True
        assert target.read_text() == "written"

    async def test_overwrite_false_still_refuses_existing_file(self, sandbox):
        root, _ = sandbox
        existing = root / "existing.txt"
        existing.write_text("original")

        result = await files_mod.write_file(path=str(existing), content="new", overwrite=False)

        assert result["success"] is False
        assert result["error"]["code"] == "PERMISSION_DENIED"
        assert existing.read_text() == "original"
