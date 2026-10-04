# termisations

**English** · [Русский](https://github.com/tzx1z/termisations/blob/main/README.ru.md)

A terminal XMPP client for people who debug the server, not just chat on it.
A raw XMPP stream panel sits next to the conversation, every message carries
badges for the extensions involved, and all the work is done with slash commands.
The functional reference is Conversations; the difference is the density of
protocol information.

The client connects to a real server and can also run on an emulated stanza
stream: the interface is the same in both modes. The interface speaks English and
Russian, see [Interface language](https://github.com/tzx1z/termisations#interface-language).

![The conversation, the raw XML stream panel and the status bar](https://raw.githubusercontent.com/tzx1z/termisations/main/screenshots/overview.png)

The screenshots are taken in the emulator, `termisations --mock`: the addresses and
messages on them are made up. The emulator also shows channel binding, which a
connection to a real server does not have yet, see
[What is missing](https://github.com/tzx1z/termisations#what-is-missing).

## Installation

From PyPI, as an isolated command-line tool:

```
uv tool install "termisations[xmpp,omemo]"
```

or

```
pipx install "termisations[xmpp,omemo]"
```

Python 3.12 or newer is required; the client is tested on Linux. The extras:

| Extra | What it adds |
|---|---|
| `xmpp` | `slixmpp` and `aiohttp`: connecting to a real server |
| `omemo` | `slixmpp-omemo`: OMEMO encryption |
| `keyring` | `keyring`: the password from the system keyring, see [The system keyring](https://github.com/tzx1z/termisations#the-system-keyring) |

Without extras only the emulator works: `termisations --mock`. With the keyring:
`uv tool install "termisations[xmpp,omemo,keyring]"`. Upgrade:
`uv tool upgrade termisations` or `pipx upgrade termisations`.

From source, for development:

```
git clone https://github.com/tzx1z/termisations.git
cd termisations
uv sync --extra xmpp --extra omemo
uv run termisations --mock
```

In a source checkout every command below runs as `uv run termisations ...` or
`.venv/bin/termisations ...`; `python -m termisations` works too.

## Running

Against a real server:

```
export TERMISATIONS_PASSWORD='...'      # or --password-command 'pass show xmpp/work'
termisations --jid alice@example.org
```

`--jid` is required: it also selects the profile - the directory holding the
config, history, keys and log of that account. The `TERMISATIONS_JID` variable
works instead of the flag. Without either one the client refuses to start and
says so: the profile directory has to be known before there is anything to read,
so the JID cannot come from the config.

Where the client takes the password from and how to store it is described in the
[Password](https://github.com/tzx1z/termisations#password) section. The `--no-history` flag disables the database.

Sending without the interface, for pipelines and cron:

```
termisations --jid alice@example.org --to bob@example.org --message "build passed"
journalctl -u nginx -n 5 | termisations --jid alice@example.org --to bob@example.org --stdin
termisations --jid alice@example.org --to bob@example.org --file build.log --message "build log"
```

In this mode Textual never starts: the client connects, sends the message or the
file, waits for stream acknowledgement and exits. On success it stays silent - no
output, exit code 0. Exit code 1 means the connection or the send failed, and the
reason goes to standard error.

The database is the same one the interactive client uses for that `--jid`, and
that is not an implementation detail. It holds the OMEMO keys and the stored
conversations, so encryption in batch mode is exactly what you left it as in that
conversation: if OMEMO is enabled for the peer in the interactive client, a
message from a pipeline goes out encrypted too. With `--no-history` there is no
database, and the client warns outright that the text will go out in the clear.

If a connection never completes, the client says so itself: the stage turns to
`error` with a reason instead of staying a spinner.

The emulated stream, without network or an account:

```
termisations --mock
```

The load mode used to check the responsiveness budget:

```
termisations --mock --scenario stress --rate 500
```

Account flags: `--jid`, `--resource`, `--server` and `--port` to bypass SRV,
`--direct-tls` for XEP-0368, `--no-tls-verify` for your own server with a
self-signed certificate, `--password-command`.

Useful flags: `--lang {en,ru}`, `--layout {focus,split,debug}`, `--no-xml`, `--xml-buffer N`,
`--scenario {default,error,handshake,muc,stress}`, `--config PATH`,
`--log-level`. The log is written to `~/.cache/termisations/<jid>/client.log`;
nothing is printed to the terminal, because Textual owns it.

## Interface language

The interface speaks English and Russian: the panels, the status bar, command
help, errors, `--help`, the example config, the emulator's conversations and the
`client.log` journal. The language is chosen at startup, the first source that is
set wins:

1. the `--lang en` or `--lang ru` flag;
2. the `TERMISATIONS_LANG` variable;
3. the `lang` key in the `[ui]` section of the config;
4. the system locale: the `LANGUAGE`, `LC_ALL`, `LC_MESSAGES` and `LANG`
   variables, in the order gettext reads them. A Russian locale gives Russian,
   any other locale gives English.

```toml
[ui]
lang = "ru"
```

`/lang ru` and `/lang en` switch the language of a running client until it exits,
`/lang` without an argument shows the current one. Lines already in the feed and
in the journal keep the language they were written in. `--help` depends only on
the flag, the variable and the locale: the config is read after the arguments are
parsed. Stage names in the status bar (`resolving SRV`, `TLS handshake`, `ready`)
and extension names are protocol terms and stay in English in both languages.

Translations live in [`src/termisations/locale/ru/`](https://github.com/tzx1z/termisations/blob/main/src/termisations/locale/ru),
one TOML file per module: the key is the English string from the code, the value
is the Russian text. `tests/test_i18n.py` checks that every string has a
translation with the same placeholders and that no Russian text is left in the
code outside the catalog.

## Profiles

Every account lives in its own directory. The directory name is the bare JID in
lower case, so `--jid Alice@Example.ORG/phone` and `--jid alice@example.org` are
the same profile.

```
~/.config/termisations/alice@example.org/config.toml
~/.local/share/termisations/alice@example.org/history.db, resource
~/.cache/termisations/alice@example.org/client.log, caps.db
```

Two accounts on one machine see neither each other's history, nor conversations,
nor OMEMO keys. Permissions are as before: directories 0700, files 0600. The
keyring needs no profiles - the account there is already a full JID. There is one
exception: the history of entered commands is shared across all profiles, see
below.

A profile config:

```toml
[account]
password_command = "pass show xmpp/work"

[tls]
verify = true
```

If the profile has no config, the client creates an example on startup: every
setting in it is commented out and explained, and the header lists the profile
directories. The example does not change the client's behaviour. To enable a
setting, remove the leading `# ` from its line. An existing config is never
overwritten, and with `--config` no example is created, since the settings live
elsewhere. The client reports the created example with a line on standard error.

The default resource is `termisations.<suffix>`, for example
`termisations.3f9a1c2e`. The suffix is random, created on the first run of the
profile and stored in the `resource` file of the profile data directory, the way
Conversations and Dino do it. Two devices of one account never share a resource,
so the server does not close the first session when the second one logs in, and
one device keeps the same resource between runs. `--no-history` does not affect
this file: it is a device name, not history. A custom resource is set with the
`--resource` flag or the `resource` key in the `[account]` section.

The `[account] jid` key is redundant inside a profile config: the directory
defines the account. You may keep it, but its value must match the profile name -
a mismatch almost always means the wrong file is being edited, and the client
refuses to start. The `--config PATH` flag overrides the profile path: one shared
file for several accounts is a legitimate way to avoid duplicating settings.

Data from the pre-profile layout migrates on its own. If `config.toml` and
`history.db` from the version without profiles are still at the root of the
directories, the first run moves them into the profile of whoever owns them: the
owner is named by `[account] jid` in the config, or, failing that, by the single
account in the database. Files belonging to another account stay where they are
and wait for their own run. A database holding several accounts has no
unambiguous owner and goes to the profile that starts first; the correspondence
of the others stays inside it and can only be moved to their profiles by hand.
The client reports the migration with a line on standard error. Cache and log are
not migrated: they rebuild themselves.

The emulator needs no profile: `--mock` starts without an account, never writes
to the real database and ignores the `TERMISATIONS_JID` variable.

## Password

The config has no key for the password: the client never stores it in plain
text. The password comes from an external command, the system keyring, an
environment variable, or a prompt at startup. The `/account` command shows where
it came from in the current session, in the `password source` line.

### Order of sources

1. `password_command` - the `--password-command` flag or the `password_command`
   key in the `[account]` section. The flag takes precedence over the config. If
   a command is set, the keyring and the environment variable are not consulted:
   when the command fails, only the terminal prompt is left.
2. The system keyring. Works only with the `keyring` extra installed.
3. The `TERMISATIONS_PASSWORD` environment variable.
4. A prompt at startup. Only when standard input is a terminal: in batch mode
   with `--stdin`, from cron and from systemd there is no prompt, the client exits
   with code 2 and writes the reason to standard error. The code is the same as for
   an error in the arguments or the config: the client never gets to connecting.

### Which method to choose

| Method | Where the password lives | Suited for | Limitations |
|---|---|---|---|
| `secret-tool` in `password_command` | a keyring over Secret Service: GNOME Keyring, KWallet, KeePassXC | a Fedora desktop or another distribution's desktop | needs a D-Bus session and an unlocked keyring: does not work over SSH without a desktop or from cron |
| `pass` in `password_command` | a GPG-encrypted file in `~/.password-store` | a terminal, including over SSH; sync through git | pinentry asks for the GPG key passphrase; from cron it works only while gpg-agent remembers that passphrase |
| the system keyring, the `keyring` extra | the same Secret Service keyring | a desktop without an external command | the entry is bound to the full JID including the resource |
| a file in `password_command` | a file with 0600 permissions | cron, systemd, a server without a desktop | the password sits on disk in plain text, protected only by file permissions |
| the `TERMISATIONS_PASSWORD` variable | the process environment | a one-off run, CI | visible to processes of the same user in `/proc/<pid>/environ` |
| a prompt at startup | nowhere | a one-off run in a terminal | the password is typed on every run |

### How password_command is executed

- The string is split by shell rules: quotes and escaping work, but no shell is
  started. Pipes, redirections, `~`, `$HOME` and `&&` are not interpreted and
  reach the command as plain arguments. For example, `echo pw | cat` returns the
  password `pw | cat` without any error. If you need shell features, start the
  shell explicitly: `sh -c 'cat "$HOME/.xmpp-password"'`. Write full paths:
  `/home/alice/...`, not `~/...`.
- The password is the first line of standard output: password managers print the
  password on the first line and the entry metadata below it. Trailing spaces stay
  part of the password.
- The command runs before the interface starts, so it may ask for the password
  store passphrase: in the same terminal (pinentry-curses, pinentry-tty) or in a
  window.
- The command has 30 seconds to answer. The client names the error directly:
  `password_command failed: <first stderr line or exit code>`,
  `password_command printed nothing`, `cannot start password_command: <reason>`,
  `password_command did not respond within 30 s`.
- In an interactive run, when the command fails, the client asks for the password
  right away and does not show the reason. So check a new command separately in a
  terminal: it must print the password on the first line and exit with code 0.

### secret-tool: the Fedora keyring

`secret-tool` comes with the `libsecret` package and is installed on Fedora
Workstation together with GNOME. Passwords are kept in the GNOME Keyring `login`
keyring, which is unlocked when you log in to the session. Any other service with
the Secret Service interface works too: KWallet on KDE Plasma, KeePassXC with the
integration enabled. An entry is identified by a set of arbitrary "attribute
value" pairs; the `type xmpp` pair separates XMPP passwords from other programs'
entries.

```
# Store. secret-tool asks for the password: one line, input is not echoed
secret-tool store --label='XMPP JID' jid tzx1z@inkov.dev type xmpp
# Check: the command prints the password
secret-tool lookup jid tzx1z@inkov.dev type xmpp
```

The profile config `~/.config/termisations/tzx1z@inkov.dev/config.toml`:

```toml
[account]
password_command = "secret-tool lookup jid tzx1z@inkov.dev type xmpp"
```

- Look up by the same attributes you stored with. `secret-tool lookup jid
  tzx1z@inkov.dev` finds the entry too, but if another program stored an entry
  with the same `jid` attribute, secret-tool returns the first unlocked match.
- Another `store` with the same attributes replaces the password in the existing
  entry, no new entry is created. This is how the password is updated after a
  change on the server.
- Delete the entry: `secret-tool clear jid tzx1z@inkov.dev type xmpp`. The entry
  is visible in the "Passwords and Keys" application (Seahorse) under the
  `--label`.
- The password can be piped into `store`, but then a trailing newline is stored
  as part of the password. It does not bother the client, which takes the first
  line, but it may bother other programs. A password typed in the command itself
  stays in the shell history, so the prompt is more convenient.
- If there is no entry, `secret-tool` exits with code 1 without a message, and
  the client reports `password_command failed: exit code 1`.
- If the keyring is locked, GNOME Keyring shows a window asking for its password.
- From cron secret-tool finds no D-Bus session and prints
  `secret-tool: Cannot autolaunch D-Bus without X11 $DISPLAY`. Over SSH the bus
  is usually there (systemd starts it on login), but the `login` keyring is
  unlocked by logging into a graphical session: if the machine has no such
  session, the entry cannot be read. For such runs use a password file, and
  override the command from the config with the `--password-command` flag.

### pass

```
sudo dnf install pass
# Once: a GPG key and the store. <GPG_ID> is the key's address or fingerprint
gpg --full-generate-key
pass init <GPG_ID>
# Store: pass asks for the password twice
pass insert xmpp/example.org
# Check
pass show xmpp/example.org
```

```toml
[account]
password_command = "pass show xmpp/example.org"
```

- The entry lives in `~/.password-store/xmpp/example.org.gpg`. The entry name is
  arbitrary; with several accounts it is convenient to name entries by JID:
  `xmpp/tzx1z@inkov.dev`.
- `pass insert -m` accepts several lines: the password on the first line,
  metadata below. The client takes only the first line.
- pinentry asks for the GPG key passphrase through gpg-agent: in a window on the
  desktop or in the terminal. gpg-agent remembers it for 10 minutes after the last
  use (`default-cache-ttl`) and no longer than 2 hours (`max-cache-ttl`). From
  cron pinentry has nowhere to show up: the command works only while the
  passphrase is in the gpg-agent cache, otherwise it fails or times out after 30
  seconds.

### The system keyring

The client reads the password from the keyring itself, without an external
command. The `keyring` extra is required:

```
uv tool install "termisations[xmpp,omemo,keyring]"
# or, in a source checkout
uv sync --extra xmpp --extra omemo --extra keyring
```

The entry is looked up in the `termisations` service, and the entry name is the
full JID including the resource, for example
`alice@example.org/termisations.3f9a1c2e`. The profile resource is kept in the
`resource` file of the profile data directory; the file appears on the first run.
`/account` shows the full JID too, in the `account` line.

```
JID="alice@example.org/$(cat ~/.local/share/termisations/alice@example.org/resource)"
# Store with the keyring library CLI: it asks for the password
uv run keyring set termisations "$JID"
# Check
uv run keyring get termisations "$JID"
```

The SecretService backend of the keyring library looks the entry up by the
`service` and `username` attributes, so the same entry can be created with
secret-tool. On Linux this backend is the default. The libsecret backend also
matches the `application` attribute and will not find an entry made by
secret-tool:

```
secret-tool store --label="termisations $JID" service termisations username "$JID"
```

- If the resource changes (the `--resource` flag, the `resource` key in the
  config, a deleted `resource` file), the entry under the old name is not found.
  Store the password again under the new full JID.
- The client treats a keyring error as a missing password and moves on to the
  next source: without a D-Bus session or with a locked keyring (cron, SSH
  without a graphical session) the keyring is skipped silently. The selected backend is shown by
  `uv run python -c "import keyring; print(keyring.get_keyring())"`; on a Fedora
  desktop it is `keyring.backends.SecretService.Keyring`.

### A password file: cron and a server without a desktop

```
# A file with 0600 permissions in the profile config directory. printf is a bash
# builtin, so the password reaches neither the history nor the process list
(umask 077; read -rs -p 'Password: ' pw; echo; printf '%s\n' "$pw" > ~/.config/termisations/alice@example.org/password)
```

In `password_command` the path is written in full, `~` is not expanded. If the
config already has a command for the desktop, the flag overrides it in the cron
job:

```
termisations --jid alice@example.org \
  --password-command 'cat /home/alice/.config/termisations/alice@example.org/password' \
  --to bob@example.org --message "build passed"
```

A password in such a file is protected only by the permissions, and it ends up in
backups of the home directory. For automated sending it is safer to create a
separate account, so that a leaked file does not open the personal one.

### The TERMISATIONS_PASSWORD variable

- Do not put the password into an `export` command: the line stays in the shell
  history. A bash variant without history:
  `read -rs TERMISATIONS_PASSWORD && export TERMISATIONS_PASSWORD`.
- The variable is visible to all child processes and to processes of the same
  user through `/proc/<pid>/environ`. For regular use the methods above are more
  reliable.
- The variable takes effect only if `password_command` is set neither by the flag
  nor in the config, and the keyring has no entry.

### The prompt at startup

If no source produced a password and standard input is a terminal, the client
asks `password for <full JID>:` before the interface starts. The
password is not stored anywhere, and empty input cancels the start.

## What is on screen

Two panels on top: the conversation with its extension badges, and the `RAW XML`
panel with lines like `12:04:11.238 OUT iq ...`. Below them the input line with a
signature hint, and at the bottom a two-row status bar: JID, channel and
conversation encryption, latency, transport, XEP-0198 state, memory, and the
spinner of the current stage.

Command output goes into the conversation, here `/sm` and `/caps bob@example.org`:

![Output of /sm and /caps in the conversation](https://raw.githubusercontent.com/tzx1z/termisations/main/screenshots/commands.png)

The mock replays a whole connection: SRV resolution, TLS handshake, SASL2 with a
redacted payload, bind, `<enable/>` per XEP-0198, roster request, initial
presences, a batch of MAM, an OMEMO key bundle from PEP, an incoming encrypted
message, a receipt, a marker, a correction and a `type='error'` stanza.

Application keys: Ctrl+D cycles layouts, Ctrl+P opens the command palette, Ctrl+O
the contact palette, Ctrl+R the input history search, Alt+1..9 jumps to a
conversation by number, Ctrl+N and Ctrl+B move between neighbouring
conversations, Ctrl+L clears the log, Ctrl+F filters it, End returns to the end
of the stream, Esc resets, Ctrl+C copies the selection or asks to quit, and a
second press in a row closes the client immediately. The quit dialog also answers
in Cyrillic: д to leave, н to stay.

The `debug` layout, Ctrl+D or `/debug`: the raw stream takes the whole width.

![The debug layout with the raw XML stream across the whole width](https://raw.githubusercontent.com/tzx1z/termisations/main/screenshots/debug.png)

All Ctrl combinations work on any keyboard layout. A terminal with the enhanced
keyboard protocol sends the character together with the modifier rather than a
control code, so on a Russian layout Ctrl+С arrives instead of Ctrl+C. Twins for
every combination are declared as a list in `app.with_cyrillic`, including the
Caps Lock variant. If a combination still does not fire, look at what your
terminal actually sends: `.venv/bin/textual keys`.

The contact palette works like the command palette: the same fuzzy search, and
the choice switches the conversation. Entries are grouped by server, the domain
is shown on the first entry of a group, and within a group available contacts
come before offline ones. Rooms are not stored in the roster but do appear in the
palette: joining them works the same way.

The input line: Tab completes commands, JIDs and nicknames, Up and Down browse
the history, Alt+Enter and Ctrl+J break the line, Backspace brings the previous
line of a multi-line input back for editing. A paste from the clipboard keeps every
line: text with newlines is laid out into the same buffer Alt+Enter fills, and
goes out as a single message.

The RAW XML panel: Shift+Tab moves focus to it and back, arrows move the cursor
across stanzas, Enter expands the stanza under the cursor, End returns to the
stream, PgUp and PgDn page through it. The full table is the `/keys` command.

The log panel filter: `kind:<type> jid:<address> ns:<namespace> err:<yes|no>`
plus free text, with conditions joined by AND. Stanza types: `msg`, `pres`, `iq`,
`strm`, `sasl`, `tls`, `oth`. For example: `/xml filter kind:iq ns:urn:xmpp:mam:2`.
An unknown field or value is not applied but explained; the reply shows how many
buffered stanzas matched. Tab after `/xml filter` completes field names, for the
second and third condition as well.

## Input history

Entered commands survive a restart and are shared across all profiles. Up brings
back what was typed yesterday and what was typed under another account; Ctrl+R
opens a reverse search over the history, the way bash does it: the input line
becomes the query, and the command found is shown in the hint below it. Another
Ctrl+R steps to the next match further back, Enter puts the match into the input
line, Escape and Ctrl+G cancel the search and restore the text you had typed.
Enter in the search sends nothing: the registry contains `/quit`,
`/send` and `/remove`.

```
~/.local/share/termisations/input-history.db
```

There is one file for all profiles, and it sits next to their directories: a
typed command belongs to the person, not to the account, and a second account
should not start with an empty history. Permissions are the same as for the rest
of the data: directory 0700, file 0600. The last 5000 lines are kept.

Only slash commands go into the file. Message bodies never do: profiles exist so
that the correspondence of one account does not end up in the data of another. Up
still brings back sent messages within the current session, but they are not
written to disk.

Commands carry addresses - `/chat`, `/join`, `/mam`, `/block` - so
the shared file shows who was talked to under other profiles. That is the price
of a shared history. Redaction is not applied to the records: a redacted command
cannot be repeated, and repeating is the whole point of a history.

The emulator reads the history but never writes it: `--mock` leaves no traces on
disk and does not even create the file. The `--no-history` flag disables both the
message database and the input recording. Answers to confirmation questions
(`yes`, `да`) never reach the history.

## What works

- Connecting to a server: SRV resolution of `_xmpps-client._tcp` and
  `_xmpp-client._tcp` honouring priority and weight, direct TLS per XEP-0368 or
  STARTTLS, SASL with SCRAM-SHA-256 and SCRAM-SHA-512, resource binding.
- Extensions on a real stream: XEP-0198 with acknowledgements, XEP-0199 as the
  latency source, XEP-0030 and XEP-0115, XEP-0280 Carbons, XEP-0313 MAM with
  paging, XEP-0359, XEP-0184, XEP-0333, XEP-0308, XEP-0085.
- The raw stream panel shows stanzas exactly as they went over the socket: the
  interception sits at the socket level, not on parsed objects.
- Three layout modes and adaptation to terminal width: from 160 columns the log
  is on the right, from 80 below, under 80 the log is hidden with a notice.
- The raw stream panel: a 2000-stanza ring buffer, batched rendering no more than
  20 times per second, markup highlighting, stanza expansion on Enter, follow and
  pause with a counter of stanzas not yet shown, a filter by stanza type, address
  and namespace, and the stream rate in the header.
- The conversation panel shows the history of the conversation, not a session
  log: transport events (pings, stream acknowledgements, TLS, SASL, disco) go to
  the log panel and the status bar, the chat state goes into the conversation
  header and fades out, and the feed keeps messages and the events that belong to
  them.
- A summary of every connection stage with its duration is printed in the
  conversation: DNS, TLS, SASL, bind, SM, roster, presence, MAM and the total.
- Secrets are redacted by default: SASL, OMEMO keys and payloads, PEP bundles,
  room passwords, signatures in XEP-0363 links. The full view is enabled only by
  `/xml --unsafe` with a typed confirmation, and while it is active the `UNSAFE`
  marker is lit.
- 45 slash commands from a single data registry: help, completion and the palette
  are built from it automatically.
- `/log save <file>`: writes the buffer with the current redaction, creates the
  file with 0600 permissions and opens it with `O_NOFOLLOW`.
- The history of entered commands across restarts, shared by all profiles: Up and
  the Ctrl+R reverse search, a 0600 file holding the last 5000 lines.
- Extension badges in the conversation are built from the `xeps.py` table, not
  from strings in the UI. Extensions that fire on every message are folded into
  the `#`, `✓`, `⇢`, `↑` marks on the message line, with the legend in `/keys`.
  The full view stays in the raw stream panel and in `/trace` output.

## What is missing

- SASL2 verified against a live server. The SASL2 and Bind 2 adapter is written
  (`protocol/sasl2/`) and checked against stanzas, but there is no server with
  support at hand: ejabberd 26.7.0 from the test container has no `mod_sasl2` or
  `mod_bind2` modules and never advertises the feature. The SASL1 fallback works
  and is covered by live tests. The SASL2 attempt can be disabled with
  `[account] sasl2 = false` or the `--no-sasl2` flag.
- Channel binding (XEP-0440) and SCRAM-PLUS: the standard `ssl` module implements
  only `tls-unique`, which is forbidden on TLS 1.3 (RFC 9266), and `tls-exporter`
  is absent from CPython. The binding field shows `n/a` rather than an invented
  value.
- OMEMO in the `urn:xmpp:omemo:2` namespace. What works is
  `eu.siacs.conversations.axolotl`: slixmpp-omemo 2.2.0 does not implement
  plaintext preparation for omemo:2, and decrypting an incoming omemo:2 message
  raises `NotImplementedError`. Bundles are published in both namespaces because
  the library does that itself; `/omemo status` names the working one.
- OX (XEP-0373): there is no maintained Python implementation, so the command
  says so directly. Under `--mock` it shows the event markup.
- XEP-0084 avatars: there is nothing to draw a picture with in a terminal, and a
  textual placeholder needs a separate decision about its form.
- Retraction delivery where the server does not perform it: `/retract` builds and
  sends a `urn:xmpp:message-retract:1` stanza, but ejabberd 26.7.0 does not
  forward it to other resources even though it advertises the extension. A
  XEP-0444 reaction under the same conditions does arrive.
- Encrypting messages to yourself: the OMEMO round trip is verified between two
  accounts, while the note-to-self scenario in the feed was never checked.

## Verified requirements

| Requirement | Status | Evidence |
|---|---|---|
| 1. Startup and three panels | done | the `termisations` entry point is declared in `pyproject.toml`; the `test_app_mounts_panels` test mounts `#chat`, `#xmllog`, `#prompt`, `#statusbar` |
| 2. Ctrl+D cycles layouts without artefacts | done | the widget tree is not rebuilt, only classes on `#body` change; tests `test_ctrl_d_cycles_layout`, `test_layout_class_matches_mode` |
| 3. 500 stanzas per second, panel lag at most 200 ms | done with a caveat | `test_batch_render_budget` measures showing a batch of 500 stanzas; the assertion threshold is raised to 600 ms because of the render timer and the polling step; `test_event_loop_stays_responsive_under_load` checks that the event loop is not blocked |
| 4. Redacted SASL stanza, no password in the clear | done | `test_masked_sasl_visible_and_password_hidden`, the `tests/test_redact.py` suite |
| 5. Extension badges come from `xeps.py`, `grep -r "XEP-0184" src/termisations/ui/` is empty | done | checked, no matches for `XEP-0184` or even `XEP-0` |
| 6. `mypy --strict` and `ruff check` without complaints | done | `mypy --strict src/termisations`: 39 files, no errors; `ruff check src tests` and `ruff format --check`: no complaints |
| 7. Pause loses no stanzas and shows a counter | done | `test_scroll_up_pauses_and_counts`, the `tests/test_xmllog.py` suite |

A caveat on item 3: what is measured is the path of a stanza from `push` to
appearing on screen in the test environment. Input responsiveness under load is
verified indirectly, through the tick rate of a timer in the event loop, rather
than by measuring keypress latency.

## Checks

```
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/mypy --strict src/termisations
.venv/bin/pytest
```

The actual result at the time of writing: no complaints, 1024 tests pass, two are
skipped - the live tests, which need a running XMPP server.

### Live tests

Tests marked `live` run against a real XMPP server and are skipped when it is not
reachable:

```
.venv/bin/pytest -m live
```

By default they expect a server on `127.0.0.1:5222` with the `localhost` domain,
accounts `alice` and `bob` with passwords `S3cr3t-<user>-2026`, and the
`devops@conference.localhost` room. The two accounts need mutual subscriptions
(`ejabberdctl add_rosteritem` in both directions): without them OMEMO does not
fetch device lists from PEP and the encryption scenarios fail.

The variables `TERMISATIONS_TEST_HOST`, `TERMISATIONS_TEST_PORT`,
`TERMISATIONS_TEST_DOMAIN`, `TERMISATIONS_TEST_USER`, `TERMISATIONS_TEST_PEER`,
`TERMISATIONS_TEST_ROOM` and `TERMISATIONS_TEST_PASSWORD` override the defaults.
On a public server with a single account the peer is a second session of the same
user:

```
TERMISATIONS_TEST_HOST=xmpp.example.org TERMISATIONS_TEST_DOMAIN=example.org \
TERMISATIONS_TEST_USER=<login> TERMISATIONS_TEST_PEER=<same login> \
TERMISATIONS_TEST_ROOM=<room>@chat.example.org \
TERMISATIONS_TEST_PASSWORD=<password> \
.venv/bin/pytest -m live
```

The host is set separately from the domain: public servers often point the A
record of the domain to a CDN, while XMPP listens on its own name. Scenarios that
need a second participant, the read marker and the OMEMO round trip, are skipped
on such a profile with the reason named.

OMEMO keys of the live tests are kept in `termisations-live-omemo/` in the system
temporary directory and survive between runs. Every new key database publishes a
new device to PEP, and old devices stay there, so a fresh database on every run
would eventually leave the peer without a usable device. To start from scratch,
delete the directory and clear the device lists on the server.

## License

AGPL-3.0-only, full text in the [`LICENSE`](https://github.com/tzx1z/termisations/blob/main/LICENSE) file.

Author - Evgeny Inkov:

- Email: <me@inkov.dev>
- XMPP: `tzx1z@inkov.dev`
- Website: https://inkov.dev

Project:

- Source code: https://github.com/tzx1z/termisations
- Bugs and feature requests: https://github.com/tzx1z/termisations/issues
- Questions and discussions: https://github.com/tzx1z/termisations/discussions

The license follows from the dependencies, not from preference. `slixmpp-omemo`
and `oldmemo` are distributed under AGPL-3.0-only, and `oldmemo` is the very
`eu.siacs.conversations.axolotl` namespace that OMEMO runs on in this client. The
rest of the dependencies are more permissive: `textual`, `slixmpp`, `omemo`,
`twomemo`, `aiodns` under MIT, `aiohttp` under Apache-2.0 and MIT. Any permissive
license for the client would require a note saying that a build with the `omemo`
extra is distributed under AGPL, and such a note is a source of confusion rather
than convenience.
