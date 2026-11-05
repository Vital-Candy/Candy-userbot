"""Тесты установщика ``installer.sh`` и скрипта публикации ``tools/publish.sh``.

Настоящие git/gh/pip не трогаем: в PATH кладём заглушки, а проект копируем во
временную папку, чтобы подмешивать в него «плохие» файлы.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Тестовые «личные данные» собираются из частей: иначе сам файл теста попал бы под проверки
# publish.sh (почта, запретные слова) и заблокировал бы настоящую публикацию.
WORD = "секрет" + "ноеимя"
EMAIL = "some" + "one@exam" + "ple.org"
BASH = shutil.which("bash")
GIT = shutil.which("git")


def _write_exec(path: Path, text: str) -> None:
    """Создаёт исполняемый файл (заглушку команды)."""
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _copy_project(dest: Path) -> None:
    """Копирует проект без .git, данных и кэша."""
    shutil.copytree(
        ROOT, dest,
        ignore=shutil.ignore_patterns(".git", "data", "logs", "__pycache__", ".venv", ".env", "*.zip"),
    )


@unittest.skipUnless(BASH and GIT, "нужны bash и git")
class PublishScriptTests(unittest.TestCase):
    """``tools/publish.sh``: анонимный автор, проверки на личные данные, защита от перезаписи."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="candy-pub-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = self.tmp / "src"
        _copy_project(self.src)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.forbidden = self.tmp / "forbidden"
        self.forbidden.write_text(WORD + "\n", encoding="utf-8")
        self.work = self.tmp / "work"

    def run_publish(self, *args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        """Запускает скрипт из копии проекта с изолированным HOME и заглушками в PATH."""
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.home),
            "PUBLISH_LOGIN": "Tester",
            "PUBLISH_ID": "12345",
            "PUBLISH_FORBIDDEN_FILE": str(self.forbidden),
            "PUBLISH_WORK": str(self.work),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
        env.update(extra_env or {})
        return subprocess.run(
            [BASH, str(self.src / "tools" / "publish.sh"), *args],
            env=env, capture_output=True, text=True, timeout=120,
        )

    def test_dry_run_creates_anonymous_single_commit(self) -> None:
        result = self.run_publish("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        log = subprocess.run(
            ["git", "log", "--format=%an <%ae>|%cn <%ce>"], cwd=self.work,
            capture_output=True, text=True, check=True,
        ).stdout.split()
        self.assertEqual(len(log), 3)  # одна строка: «Имя <адрес>|Имя <адрес>» из трёх слов
        self.assertEqual(set("".join(log).split("|")), {"Candy<12345+Tester@users.noreply.github.com>"})
        commits = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"], cwd=self.work, capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(commits, "1")

    def test_copy_has_no_git_history_data_or_cache(self) -> None:
        (self.src / "data").mkdir()
        (self.src / "data" / "x.txt").write_text("личное", encoding="utf-8")
        (self.src / ".env").write_text("A=1\n", encoding="utf-8")
        self.assertEqual(self.run_publish("--dry-run").returncode, 0)
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=self.work, capture_output=True, text=True, check=True,
        ).stdout.split("\n")
        self.assertIn("main.py", tracked)
        self.assertFalse([name for name in tracked if name.startswith("data/") or name == ".env"])

    def test_running_install_is_refused_unless_forced(self) -> None:
        session = self.src / "data" / "accounts" / "1" / "main.session"
        session.parent.mkdir(parents=True)
        session.write_bytes(b"x")
        result = self.run_publish("--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("рабочую установку", result.stdout + result.stderr)
        self.assertFalse(self.work.exists())
        forced = self.run_publish("--dry-run", extra_env={"PUBLISH_FROM_INSTALL": "1"})
        self.assertEqual(forced.returncode, 0)

    def test_forbidden_word_blocks_publication(self) -> None:
        (self.src / "README.md").write_text("Автор: " + WORD.capitalize() + "\n", encoding="utf-8")
        result = self.run_publish("--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("запретные слова", result.stderr)

    def test_forbidden_word_in_file_name_blocks_publication(self) -> None:
        (self.src / (WORD + ".txt")).write_text("x", encoding="utf-8")
        result = self.run_publish("--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("именах файлов", result.stderr)

    def test_email_blocks_publication_but_noreply_is_allowed(self) -> None:
        (self.src / "NOTES.md").write_text("1+me@users.noreply.github.com\n", encoding="utf-8")
        self.assertEqual(self.run_publish("--dry-run").returncode, 0)
        shutil.rmtree(self.work)
        (self.src / "NOTES.md").write_text("пишите на " + EMAIL + "\n", encoding="utf-8")
        result = self.run_publish("--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("адреса электронной почты", result.stderr)

    def test_secret_blocks_publication(self) -> None:
        (self.src / "cfg.py").write_text('api_hash = "' + "a1" * 16 + '"\n', encoding="utf-8")
        result = self.run_publish("--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("секреты", result.stderr)

    def test_phone_number_and_api_id_block_publication(self) -> None:
        # Строки собираются по частям: иначе сам тест выглядел бы как утечка и блокировал публикацию.
        for text in ("звоните +7916" + "1234567\n", "phone = 7916" + "1234567\n", "api_id = 12345" + "67\n"):
            with self.subTest(text=text):
                (self.src / "NOTES.md").write_text(text, encoding="utf-8")
                shutil.rmtree(self.work, ignore_errors=True)
                result = self.run_publish("--dry-run")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("номера телефонов", result.stderr)
        (self.src / "NOTES.md").write_text("пример: phone=998901234567 и api_id=12345\n", encoding="utf-8")
        shutil.rmtree(self.work, ignore_errors=True)
        self.assertEqual(self.run_publish("--dry-run").returncode, 0)  # выдуманный номер тестов и короткий id разрешены

    def test_personal_files_block_publication(self) -> None:
        for name in ("dump.db", "candy.log", "key.pem", "downloads/photo.jpg", ".env.local"):
            with self.subTest(name=name):
                target = self.src / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("x", encoding="utf-8")
                shutil.rmtree(self.work, ignore_errors=True)
                result = self.run_publish("--dry-run")
                self.assertNotEqual(result.returncode, 0, name)
                self.assertIn("личные или служебные файлы", result.stderr)
                target.unlink()

    def test_name_from_git_config_is_forbidden_automatically(self) -> None:
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Zxcv" + "bnm Qwer" + "ty\n\temail = zxcv" + "bnm.q@example" + ".org\n", encoding="utf-8")
        env = {"GIT_CONFIG_GLOBAL": str(gitconfig)}
        self.assertEqual(self.run_publish("--dry-run", extra_env=env).returncode, 0)
        shutil.rmtree(self.work)
        (self.src / "README.md").write_text("Автор: zxcv" + "bnm\n", encoding="utf-8")
        result = self.run_publish("--dry-run", extra_env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("запретные слова", result.stderr)

    def test_default_work_dir_is_reused_and_removed_after_release(self) -> None:
        remote = self.make_remote("0.0.1")
        self.stub_gh_for(remote)
        default = self.home / ".candy-publish"
        env = {"PUBLISH_WORK": ""}
        self.assertEqual(self.run_publish("--release", "--dry-run", extra_env=env).returncode, 0)
        self.assertTrue(default.is_dir())  # пробный запуск оставляет копию для просмотра
        again = self.run_publish("--release", "--dry-run", extra_env=env)  # второй запуск перезаписывает ту же папку
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual([item.name for item in self.home.iterdir()], [".candy-publish"])
        done = self.run_publish("--release", extra_env=env)
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertEqual(list(self.home.iterdir()), [])  # после отправки ничего не осталось

    def test_old_publish_folders_of_this_script_are_cleaned_but_foreign_ones_stay(self) -> None:
        mine = self.home / "candy-publish-20260101-000000"
        theirs = self.home / "candy-publish-20260102-000000"
        for folder, author in ((mine, "Candy"), (theirs, "Somebody")):
            folder.mkdir()
            subprocess.run([GIT, "init", "-q"], cwd=folder, check=True)
            subprocess.run([GIT, "-c", f"user.name={author}", "-c", "user.email=a@users.noreply.github.com",
                            "commit", "-q", "--allow-empty", "-m", "x"], cwd=folder, check=True)
        result = self.run_publish("--dry-run", extra_env={"PUBLISH_WORK": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(mine.exists())
        self.assertTrue(theirs.exists())

    def test_default_work_dir_that_is_not_ours_is_refused(self) -> None:
        (self.home / ".candy-publish").mkdir()
        (self.home / ".candy-publish" / "mine.txt").write_text("важное", encoding="utf-8")
        result = self.run_publish("--dry-run", extra_env={"PUBLISH_WORK": ""})
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.home / ".candy-publish" / "mine.txt").exists())

    def test_real_run_requires_forbidden_file(self) -> None:
        self.forbidden.unlink()
        result = self.run_publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("запретных слов", result.stderr)

    def test_existing_work_dir_is_refused(self) -> None:
        self.work.mkdir()
        result = self.run_publish("--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("уже существует", result.stderr)

    def test_existing_remote_repo_is_not_touched(self) -> None:
        # Заглушка gh: «репозиторий есть». Любая другая команда gh (create) — ошибка теста.
        _write_exec(self.bin / "gh", '#!/bin/sh\n[ "$1 $2" = "repo view" ] && exit 0\necho "unexpected gh $*" >&2\nexit 9\n')
        result = self.run_publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("уже существует", result.stderr)
        self.assertNotIn("unexpected gh", result.stderr)

    def test_real_run_creates_repo_via_gh_and_pushes_tag(self) -> None:
        calls = self.tmp / "gh-calls.txt"
        _write_exec(
            self.bin / "gh",
            f'#!/bin/sh\necho "$@" >> "{calls}"\n[ "$1 $2" = "repo view" ] && exit 1\n'
            # «create --push» заглушка не делает: репозиторий-приёмник заменяет origin ниже.
            'exit 0\n',
        )
        # Принимающий репозиторий для git push тега.
        remote = self.tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        wrapper_dir = self.tmp / "gitwrap"
        wrapper_dir.mkdir()
        _write_exec(
            wrapper_dir / "git",
            f'#!/bin/sh\nif [ "$1" = "push" ]; then exec {GIT} push -q "{remote}" "$4"; fi\nexec {GIT} "$@"\n',
        )
        result = self.run_publish(extra_env={"PATH": f"{wrapper_dir}{os.pathsep}{self.bin}{os.pathsep}{os.environ['PATH']}"})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        text = calls.read_text(encoding="utf-8")
        self.assertIn("repo create Vital-Candy/Candy-userbot --public", text)
        tags = subprocess.run(
            [GIT, "--git-dir", str(remote), "tag"], capture_output=True, text=True, check=True,
        ).stdout.split()
        self.assertEqual(len(tags), 1)
        self.assertTrue(tags[0].startswith("v"))

    def make_remote(self, version: str, *, author: str = "Candy <12345+Tester@users.noreply.github.com>") -> Path:
        """Bare-репозиторий с «уже опубликованной» версией и устаревшим файлом."""
        seed = self.tmp / "seed"
        (seed / "core").mkdir(parents=True)
        (seed / "core" / "version.py").write_text(f'CORE_VERSION = "{version}"\n', encoding="utf-8")
        (seed / "old.txt").write_text("устарел", encoding="utf-8")
        name, email = author.split(" <")
        env = {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email.rstrip(">"),
               "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email.rstrip(">"),
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
        for command in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "first"]):
            subprocess.run(["git", *command], cwd=seed, env=env, check=True, capture_output=True)
        remote = self.tmp / "remote.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(remote)], check=True, capture_output=True)
        return remote

    def stub_gh_for(self, remote: Path) -> None:
        """gh: репозиторий «есть», а ``repo clone`` клонирует наш bare-репозиторий."""
        _write_exec(
            self.bin / "gh",
            f'#!/bin/sh\nif [ "$1 $2" = "repo view" ]; then exit 0; fi\n'
            f'if [ "$1 $2" = "repo clone" ]; then exec {GIT} clone -q "{remote}" "$4"; fi\nexit 9\n',
        )

    def test_release_adds_one_commit_on_top_and_pushes_tag(self) -> None:
        remote = self.make_remote("0.0.1")
        self.stub_gh_for(remote)
        result = self.run_publish("--release")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        count = subprocess.run([GIT, "--git-dir", str(remote), "rev-list", "--count", "main"],
                               capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(count, "2")  # старый коммит остался, добавлен один новый
        files = subprocess.run([GIT, "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                               capture_output=True, text=True, check=True).stdout.split("\n")
        self.assertIn("main.py", files)
        self.assertNotIn("old.txt", files)  # удалённое в проекте исчезло и из репозитория
        authors = subprocess.run([GIT, "--git-dir", str(remote), "log", "--format=%an <%ae>|%cn <%ce>", "main"],
                                 capture_output=True, text=True, check=True).stdout.split("\n")
        self.assertEqual({a for line in authors if line for a in line.split("|")},
                         {"Candy <12345+Tester@users.noreply.github.com>"})
        tags = subprocess.run([GIT, "--git-dir", str(remote), "tag"], capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(len(tags), 1)

    def test_release_dry_run_pushes_nothing(self) -> None:
        remote = self.make_remote("0.0.1")
        self.stub_gh_for(remote)
        self.assertEqual(self.run_publish("--release", "--dry-run").returncode, 0)
        count = subprocess.run([GIT, "--git-dir", str(remote), "rev-list", "--count", "main"],
                               capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(count, "1")

    def test_release_requires_higher_version(self) -> None:
        current = re.search(r'^CORE_VERSION = "([^"]+)"', (self.src / "core" / "version.py").read_text(encoding="utf-8"), re.M).group(1)
        for published in (current, "99.0.0"):
            with self.subTest(published=published):
                shutil.rmtree(self.tmp / "seed", ignore_errors=True)
                shutil.rmtree(self.tmp / "remote.git", ignore_errors=True)
                shutil.rmtree(self.work, ignore_errors=True)
                remote = self.make_remote(published)
                self.stub_gh_for(remote)
                result = self.run_publish("--release")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("не выше опубликованной", result.stderr)

    def test_release_refuses_foreign_author_in_history(self) -> None:
        remote = self.make_remote("0.0.1", author="Реальное Имя <real@" + "exam" + "ple.org>")
        self.stub_gh_for(remote)
        result = self.run_publish("--release")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("неожиданный автор", result.stderr)

    def test_release_without_changes_is_refused(self) -> None:
        remote = self.make_remote("0.0.1")
        self.stub_gh_for(remote)
        # Приём: публикуем тот же проект дважды с разрешением той же версии.
        self.assertEqual(self.run_publish("--release").returncode, 0)
        shutil.rmtree(self.work)
        result = self.run_publish("--release", extra_env={"PUBLISH_ALLOW_SAME_VERSION": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("нет изменений", result.stderr)

    def test_release_needs_existing_repository(self) -> None:
        _write_exec(self.bin / "gh", "#!/bin/sh\n[ \"$1 $2\" = \"repo view\" ] && exit 1\nexit 9\n")
        result = self.run_publish("--release")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("не найден", result.stderr)

    def test_help_and_bad_argument(self) -> None:
        self.assertEqual(self.run_publish("--help").returncode, 0)
        bad = self.run_publish("--wat")
        self.assertNotEqual(bad.returncode, 0)


@unittest.skipUnless(BASH and GIT, "нужны bash и git")
class InstallerTests(unittest.TestCase):
    """``installer.sh``: клонирование, установка зависимостей и защита чужих папок."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="candy-inst-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.txt"
        # git: «clone URL DIR» создаёт папку с .git и минимумом файлов проекта.
        _write_exec(
            self.bin / "git",
            f'#!/bin/sh\necho "git $@" >> "{self.log}"\n'
            'if [ "$1" = "clone" ]; then mkdir -p "$3/.git"; echo x > "$3/requirements.txt";\n'
            '  echo "#!/bin/sh" > "$3/run.sh"; echo "print(1)" > "$3/main.py"; fi\nexit 0\n',
        )
        # python3: pip и main.py подменяем, проверку версии отдаём настоящему Python.
        _write_exec(
            self.bin / "python3",
            f'#!/bin/sh\ncase "$1 $2" in\n'
            f'  "-m pip") echo "pip $@" >> "{self.log}"; exit 0;;\n'
            f'  "main.py --check") echo "check" >> "{self.log}"; exit 0;;\n'
            f'esac\nexec "{sys.executable}" "$@"\n',
        )

    def run_installer(self, directory: Path, **extra: str) -> subprocess.CompletedProcess:
        """Запускает установщик как в Termux (без apt/sudo) с заглушками."""
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.home),
            "TERMUX_VERSION": "1",
            "CANDY_DIR": str(directory),
            "CANDY_REPO": "https://example.invalid/Candy-Userbot.git",
        }
        env.update(extra)
        return subprocess.run([BASH, str(ROOT / "installer.sh")], env=env, capture_output=True, text=True, timeout=60)

    def test_fresh_install_clones_installs_and_checks(self) -> None:
        target = self.home / "Candy-Userbot"
        result = self.run_installer(target)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        calls = self.log.read_text(encoding="utf-8")
        self.assertIn("git clone https://example.invalid/Candy-Userbot.git", calls)
        self.assertIn("pip -m pip install -r requirements.txt", calls)
        self.assertIn("check", calls)
        self.assertTrue((target / "run.sh").exists())
        self.assertIn("./run.sh", result.stdout)

    def test_existing_install_is_not_cloned_again(self) -> None:
        target = self.home / "Candy-Userbot"
        (target / ".git").mkdir(parents=True)
        (target / "requirements.txt").write_text("x\n", encoding="utf-8")
        (target / "main.py").write_text("", encoding="utf-8")
        result = self.run_installer(target)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn("git clone", self.log.read_text(encoding="utf-8"))

    def test_foreign_directory_is_never_overwritten(self) -> None:
        target = self.home / "mine"
        target.mkdir()
        (target / "keep.txt").write_text("важное", encoding="utf-8")
        result = self.run_installer(target)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((target / "keep.txt").read_text(encoding="utf-8"), "важное")
        self.assertFalse(self.log.exists() and "git clone" in self.log.read_text(encoding="utf-8"))

    def test_clone_failure_stops_installer(self) -> None:
        _write_exec(self.bin / "git", '#!/bin/sh\nexit 1\n')
        result = self.run_installer(self.home / "Candy-Userbot")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Не удалось скачать", result.stderr)


if __name__ == "__main__":
    unittest.main()
