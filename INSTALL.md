# Install ocish

`ocish` requires Python 3.12 or newer, OCI credentials, and GNU Readline. It uses GNU Readline for Tab completion and `Alt-.` / `Esc .` last-argument insertion.

## macOS

```bash
xcode-select --install
brew install python@3.12 readline uv
git clone <repository-url> ocish
cd ocish
export CPPFLAGS="-I$(brew --prefix readline)/include"
export LDFLAGS="-L$(brew --prefix readline)/lib"
uv venv --python 3.12
uv sync
uv run ocish
```

`Esc .` always inserts the previous command's last argument. Configure your terminal's Option key as Meta if you want `Option-.` to send the standard `Alt-.` sequence.

## Linux

Install the Python development headers, compiler toolchain, and GNU Readline development library.

Debian/Ubuntu:

```bash
sudo apt update
sudo apt install build-essential python3.12 python3.12-dev libreadline-dev
python3.12 -m pip install --user uv
```

Fedora/RHEL:

```bash
sudo dnf install gcc python3.12 python3.12-devel readline-devel
python3.12 -m pip install --user uv
```

Then install and run ocish:

```bash
git clone <repository-url> ocish
cd ocish
uv venv --python 3.12
uv sync
uv run ocish
```

## OCI credentials

Configure the default OCI CLI profile before starting the shell:

```bash
oci setup config
```

The profile must be permitted to list and read the OCI resources you intend to browse. `ocish` reads the default profile from `~/.oci/config`.

## Verify

Start `ocish`, type `l`, then press Tab. Type `completion` to confirm the default `catalog` mode. The initial active-compartment catalog refresh runs in the background; once it completes, `ll cor<Tab>` completes to `core.` and `ll core.<Tab>` offers only collections present in that compartment.
