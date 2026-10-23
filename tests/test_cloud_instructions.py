from pathlib import Path
import tempfile
import unittest

from cloud.install_instructions import BEGIN, END, install


class CloudInstructionsTests(unittest.TestCase):
    def test_root_entry_survives_repeated_install_and_preserves_user_rules(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            project = workspace / 'project with spaces'
            project.mkdir()
            (project/'AGENTS.md').write_text('Project rules', encoding='utf-8')
            path = workspace/'AGENTS.md'
            original = 'User-owned rules; keep these exactly.\n'
            path.write_text(original, encoding='utf-8')
            path.chmod(0o640)
            self.assertEqual(install(workspace, project), path)
            first = path.read_bytes()
            install(workspace, project)
            self.assertEqual(path.read_bytes(), first)
            self.assertTrue(path.read_text().startswith(original))
            self.assertEqual(path.read_text().count(BEGIN), 1)
            self.assertIn(str(project/'AGENTS.md'), path.read_text())
            self.assertNotIn('{{PROJECT_ROOT}}', path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_inconsistent_markers_and_symlinks_preserve_existing_files(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            project = workspace/'project'
            project.mkdir()
            (project/'AGENTS.md').write_text('Project rules')
            path = workspace/'AGENTS.md'
            for original in [BEGIN, END, END + BEGIN, BEGIN + END + BEGIN]:
                path.write_text(original)
                with self.assertRaises(ValueError):
                    install(workspace, project)
                self.assertEqual(path.read_text(), original)
            path.unlink()
            path.symlink_to(project/'AGENTS.md')
            with self.assertRaises(ValueError):
                install(workspace, project)
            self.assertEqual((project/'AGENTS.md').read_text(), 'Project rules')

    def test_installs_new_entry_and_updates_only_its_owned_block(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            project = workspace/'project'
            project.mkdir()
            (project/'AGENTS.md').write_text('Project rules')
            path = install(workspace, project)
            self.assertIn('prepare', path.read_text())
            path.write_text('before\n' + BEGIN + '\nold block\n' + END + '\nafter\n')
            install(workspace, project)
            self.assertTrue(path.read_text().startswith('before\n'))
            self.assertTrue(path.read_text().endswith('\nafter\n'))
            self.assertNotIn('old block', path.read_text())
