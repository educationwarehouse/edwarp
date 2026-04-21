import hashlib
import io
import os
import re
import sys
import typing
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import dotenv
import tomlkit
import yaml
from expandvars import expandvars

from .css import extract_contents_for_css, prepend_global_css_variables
from .js import extract_contents_for_js
from .shared import truthy

DEFAULT_INPUT = "bundle.yaml"
DEFAULT_OUTPUT_JS = "bundle.js"
DEFAULT_OUTPUT_CSS = "bundle.css"
TEMP_OUTPUT = ".bundle_tmp"


def load_dotenv_once(_={}):
    if _.get("seen"):
        return False

    dotenv_path = dotenv.find_dotenv(usecwd=True)
    dotenv.load_dotenv(dotenv_path)
    _["seen"] = True
    return True


def convert_data(data: dict[str, typing.Any] | list[typing.Any] | typing.Any):
    if isinstance(data, dict):
        return {key.replace("-", "_"): convert_data(value) for key, value in data.items()}
    if isinstance(data, list):
        return [convert_data(value) for value in data]
    return data


def _load_config_yaml(fname: str):
    with open(fname) as f:
        data = yaml.load(f, yaml.SafeLoader)
    return convert_data(data)


def _load_config_toml(fname: str, key: str = ""):
    with open(fname) as f:
        data = tomlkit.load(f)

    if key:
        for part in key.split("."):
            data = data.get(part)
            if data is None:
                return {}

    return convert_data(data)


def _load_config_pyproject():
    data = _load_config_toml("pyproject.toml", key="tool.edwarp.bundle")
    data = data or _load_config_toml("pyproject.toml", key="tool.edwh.bundle")
    return "pyproject.toml", data


def _load_config(fname: str = DEFAULT_INPUT, strict=False) -> tuple[str, dict]:
    if os.path.exists(fname) and fname.endswith((".yml", ".yaml")):
        return fname, _load_config_yaml(fname)
    if os.path.exists(fname) and fname.endswith(".toml"):
        if fname == "pyproject.toml":
            return _load_config_pyproject()
        return fname, _load_config_toml(fname)
    if fname == DEFAULT_INPUT and (altname := DEFAULT_INPUT.replace(".yaml", ".toml")) and os.path.exists(altname):
        return altname, _load_config_toml(altname)
    if os.path.exists("pyproject.toml"):
        return _load_config_pyproject()
    if strict:
        raise FileNotFoundError(fname)
    return "", {}


def load_config(fname: str = DEFAULT_INPUT, strict=True, verbose=False) -> dict[str, dict]:
    file_used, data = _load_config(fname, strict=strict)

    if not data and strict:
        raise ValueError(f"Config data found for `{file_used}` was empty!")
    if verbose:
        print(f"Using config: {file_used}", file=sys.stderr)

    if data.get("configurations"):
        return data["configurations"]
    return {"_": data} if data else {}


@contextmanager
def start_buffer(temp: str | typing.IO = TEMP_OUTPUT) -> typing.IO:
    if isinstance(temp, io.IOBase):
        yield temp
        return

    path = Path(temp)
    if path.exists():
        path.unlink()

    path.parent.mkdir(parents=True, exist_ok=True)

    f = path.open("a")
    try:
        yield f
    finally:
        f.close()


def cli_or_config(
    value: typing.Any,
    config: dict,
    key: typing.Hashable,
    is_bool: bool = True,
    default: typing.Any = None,
) -> bool | typing.Any:
    return (truthy(value) if is_bool else value) if value is not None else config.get(key, default)


ENV_VAR_RE = re.compile(r"\$\{[A-Z_][A-Z0-9_]*(?:[^}]*)?}|\$[A-Z_][A-Z0-9_]*\b")


def replace_placeholders(raw_string: str) -> str:
    def replacer(match: re.Match[str]) -> str:
        placeholder = match.group(0)
        return expandvars(placeholder)

    return ENV_VAR_RE.sub(replacer, raw_string)


def _fill_variables_from_dotenv(source: str | list[str] | dict[str, typing.Any] | None) -> dict[str, typing.Any]:
    load_dotenv_once()

    if isinstance(source, str):
        source = replace_placeholders(source)
    elif isinstance(source, list):
        source = [replace_placeholders(_) for _ in source]
    elif isinstance(source, dict):
        source = {k: _fill_variables_from_dotenv(v) for k, v in source.items()}
    elif source is None:
        return {}

    return source


def _fill_variables(setting: str | dict, variables: dict[re.Pattern, str]) -> str | dict[str, str] | list[str]:
    if isinstance(setting, dict):
        return {k: _fill_variables(v, variables) for k, v in setting.items()}
    if isinstance(setting, list):
        return [_fill_variables(s, variables) for s in setting]

    if "$" not in str(setting):
        return setting

    for reg, repl in variables.items():
        setting = reg.sub(str(repl), str(setting))

    return setting


@typing.overload
def fill_variables(setting: str, variables: dict[re.Pattern, str]) -> str: ...


@typing.overload
def fill_variables(setting: dict, variables: dict[re.Pattern, str]) -> dict[str, str]: ...


def fill_variables(setting: str | dict, variables: dict[re.Pattern, str]) -> str | dict[str, str] | list[str]:
    data = _fill_variables(setting, variables)
    return _fill_variables_from_dotenv(data)


def _regexify_settings(setting_dict: dict[str, typing.Any]) -> dict[re.Pattern, typing.Any]:
    return {re.compile(rf"\${key}"): value for key, value in setting_dict.items()}


def calculate_file_hash(filename: str | Path):
    h = hashlib.sha1()
    with open(filename, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()


def store_file_hash(input_filename: str, output_filename: str = None):
    if output_filename is None:
        output_filename = f"{input_filename}.hash"
    file_hash = calculate_file_hash(input_filename)
    with open(output_filename, "w") as f:
        f.write(file_hash)
    return output_filename


class FileHandler(typing.Protocol):
    def __call__(
        self, file: dict | str, settings: dict, cache: bool = True, minify: bool = True, verbose: bool = False
    ) -> str: ...


def _handle_files(
    files: list,
    callback: FileHandler,
    output: str | typing.IO,
    verbose: bool,
    use_cache: bool,
    minify: bool,
    store_hash: bool,
    settings: dict,
    postprocess: typing.Callable[[str, dict], str] = None,
):
    re_settings = _regexify_settings(settings)

    output = fill_variables(output, re_settings)
    files = [fill_variables(f, re_settings) for f in files]
    settings = fill_variables(settings, re_settings)

    if verbose:
        print(
            f"Building {callback.__name__.split('_')[-1]} [verbose]\n{output=}\n",
            f"{minify=}\n",
            f"{use_cache=}\n",
            f"{store_hash=}\n",
            f"{files=}\n",
            file=sys.stderr,
        )

    if not files:
        if verbose:
            print("No files supplied, quitting", file=sys.stderr)
        return

    final = ""

    for inf in files:
        if not inf:
            continue

        if not minify:
            src = str(inf).replace("/*", "//").replace("*/", "")
            final += f"/* SOURCE: {src} */\n"

        res = callback(inf, settings, cache=use_cache, minify=minify, verbose=verbose)

        final += res.strip() + "\n"
        if verbose:
            print(f"Handled {inf}", file=sys.stderr)

    if postprocess:
        final = postprocess(final, settings)

    with start_buffer(output) as outputf:
        outputf.write(final)

    if verbose:
        print(f"Written final bundle to {output}", file=sys.stderr)

    if store_hash:
        hash_file = store_file_hash(output)
        return output, hash_file

    return output


def bundle_js(
    files: list[str] = None,
    verbose: bool = False,
    output: str | typing.IO = None,
    minify: bool = True,
    use_cache: bool = True,
    save_hash: bool = False,
    **settings,
) -> typing.Optional[str]:
    if output is None:
        output = io.StringIO()

    _handle_files(
        files,
        extract_contents_for_js,
        output,
        verbose=verbose,
        use_cache=use_cache,
        store_hash=save_hash,
        minify=minify,
        settings=settings,
    )

    if not isinstance(output, io.StringIO):
        return output

    output.seek(0)
    return output.read()


@dataclass
class NotFound(Exception):
    type: typing.Literal["js", "css"]

    def __str__(self):
        return f"Please specify either `files` or the `{self.type}` key in a config file (e.g. bundle.yaml)"


def bundle_css(
    files: list[str] = None,
    verbose: bool = False,
    output: str | typing.IO = None,
    minify: bool = True,
    use_cache: bool = True,
    save_hash: bool = False,
    **settings,
) -> typing.Optional[str]:
    if output is None:
        output = io.StringIO()

    _handle_files(
        files,
        extract_contents_for_css,
        output,
        verbose=verbose,
        use_cache=use_cache,
        store_hash=save_hash,
        minify=minify,
        settings=settings,
        postprocess=prepend_global_css_variables,
    )

    if not isinstance(output, io.StringIO):
        return output

    output.seek(0)
    return output.read()


def build_js(
    files: list[str] = None,
    config: str = DEFAULT_INPUT,
    verbose: bool = False,
    output: str | typing.IO = None,
    minify: bool = None,
    use_cache: bool = None,
    save_hash: bool = None,
    version: str = None,
    name: str | None = None,
):
    configs = load_config(config)

    results = {}
    for config_name, config_data in configs.items():
        if name and config_name != name:
            continue

        js_files = files or config_data.get("js")
        if not js_files:
            raise NotFound("js")

        settings = config_data.get("config", {})
        settings["version"] = cli_or_config(version, settings, "version", is_bool=False, default="latest")

        results[config_name] = _handle_files(
            js_files,
            extract_contents_for_js,
            verbose=verbose,
            output=cli_or_config(output, settings, "output_js", is_bool=False) or DEFAULT_OUTPUT_JS,
            use_cache=cli_or_config(use_cache, settings, "cache", default=True),
            store_hash=cli_or_config(save_hash, settings, "hash"),
            minify=cli_or_config(minify, settings, "minify"),
            settings=settings,
        )

    return results


def build_css(
    files: list[str] = None,
    config: str = DEFAULT_INPUT,
    verbose: bool = False,
    output: str | typing.IO = None,
    minify: bool = None,
    use_cache: bool = None,
    save_hash: bool = None,
    version: str = None,
    name: str | None = None,
):
    configs = load_config(config)

    results = {}
    for config_name, config_data in configs.items():
        if name and config_name != name:
            continue

        settings = config_data.get("config", {})
        settings["version"] = cli_or_config(version, settings, "version", is_bool=False, default="latest")

        css_files = files or config_data.get("css")
        if not css_files:
            raise NotFound("css")

        results[config_name] = _handle_files(
            css_files,
            extract_contents_for_css,
            verbose=verbose,
            output=cli_or_config(output, settings, "output_css", is_bool=False) or DEFAULT_OUTPUT_CSS,
            use_cache=cli_or_config(use_cache, settings, "cache", default=True),
            store_hash=cli_or_config(save_hash, settings, "hash"),
            minify=cli_or_config(minify, settings, "minify"),
            settings=settings,
            postprocess=prepend_global_css_variables,
        )

    return results


def build(
    config: str = DEFAULT_INPUT,
    verbose: bool = False,
    output_js: str = None,
    output_css: str = None,
    minify: bool = None,
    use_cache: bool = None,
    save_hash: bool = None,
    version: str = None,
    name: str | None = None,
):
    configs = load_config(config, verbose=True)
    result = []

    for config_name, config_dict in configs.items():
        if name and config_name != name:
            continue

        settings = config_dict.get("config", {})

        do_minify = cli_or_config(minify, settings, "minify")
        do_use_cache = cli_or_config(use_cache, settings, "cache", default=True)
        do_save_hash = cli_or_config(save_hash, settings, "hash")

        try:
            result.append(
                build_js(
                    None,
                    config,
                    verbose,
                    output_js,
                    do_minify,
                    do_use_cache,
                    do_save_hash,
                    version,
                    name=config_name,
                )
            )
        except NotFound:
            pass

        try:
            result.append(
                build_css(
                    None,
                    config,
                    verbose,
                    output_css,
                    do_minify,
                    do_use_cache,
                    do_save_hash,
                    version,
                    name=config_name,
                )
            )
        except NotFound:
            pass

    return result
