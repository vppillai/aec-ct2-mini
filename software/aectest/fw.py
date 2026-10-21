"""Module firmware update over CDB ([CMIS 5.2] 7.3.1 Firmware Update, Tables 9-9 and 9-15 to 9-25).

The path is Start (0101h) with the vendor header, Write LPL (0103h) or EPL (0104h) blocks, Complete (0107h), a
verify read of 0100h, then Run (0109h) and, on a separate explicit request, Commit (010Ah). Every CDB command carries
its CdbChkCode and waits on CdbStatus for the duration the module advertises in 0041h. Any failure before Run ends in
Abort (0102h when advertised, else Complete, which the module reads as a failed download: 7.3.1.2) and the module keeps
its committed image. The module stays in low power through the whole path: no LPMode release, no power budget question.

Guards, in the order they run (every one is a Refused; the only override is --force on the version guard):
  advertised: 01h:163 names a CDB instance and 0041h answers with WriteMechanism != none;
  quiet: 00h:3 = ModuleLowPwr, or every Data Path in DPDeactivated (11h:128-131);
  size: the file is longer than StartCmdPayloadSize and fits the U32 ImageSize;
  header: the first StartCmdPayloadSize bytes carry the module's part number (ASCII, as 00h:148-163 reads it), or the
          operator passes header_bytes = StartCmdPayloadSize to say the vendor format is known to them;
  version: the file's version (header text or --image-version) is not the running image's version, unless force;
  commit: needs the confirmation string 'COMMIT <serial>'.
The vendor header format is vendor private (7.3.1.1): the tool never decodes it beyond the two text searches above.
"""
import os
import re
import time

from .cdb import cdb_command
from .cmis import DP_STATES, L_STATE, STATES, decode_cdb_advert, decode_id
from .transport import Nack, Refused

CMD = {'start': 0x0101, 'abort': 0x0102, 'write_lpl': 0x0103, 'write_epl': 0x0104, 'complete': 0x0107, 'copy': 0x0108,
       'run': 0x0109, 'commit': 0x010A, 'features': 0x0041, 'info': 0x0100}
LPL_BLOCK = 116            # Table 9-18: 9Fh:140-255 FirmwareBlock
RUN_INACTIVE_RESET = 0x00  # Table 9-24 ImageToRun: traffic-affecting reset to the inactive image
RUN_DELAY_MS = 500         # Table 9-24 DelayToReset: time for the host to read CdbStatus before the reset
RUN_WAIT_S = 30.0          # the module answers again within this (vendor dependent; the Accelink boot is seconds)
DEFAULT_MS = {'start': 5000, 'abort': 2000, 'write': 1000, 'complete': 10000, 'copy': 10000}  # when 0041h says 0


def _say(say):
    return say or (lambda text, err=False: None)


def _status(res):
    return f"{res['status']['coarse']} ({res['status']['result']})"


PASSWORD_ENTRY = 122  # [CMIS 5.2] Table 8-10 bytes 122-125: host password entry, vendor-defined value, cleared at reset


class _SkipDownload(Exception):
    pass


def _nullcontext():
    import contextlib
    return contextlib.nullcontext()


def _pad_version(v):
    """'5.6' -> '05.06' so it matches the bcd text of 0100h ('05.06.0000')."""
    parts = str(v).split('.')
    return '.'.join(p.zfill(2) for p in parts[:2])


def fw_info(port, say=None):
    """0041h and 0100h with the lower-page firmware bytes: features, images, the active and inactive versions."""
    say = _say(say)
    with port.selected():
        low = port.read(0, 128)
        p1 = port.read_page(0x01)
        adv = decode_cdb_advert(p1)
        out = {'advert': adv, 'active_fw': f'{low[39]}.{low[40]}', 'inactive_fw': f'{p1[0]}.{p1[1]}',
               'module_state': STATES.get(low[L_STATE] >> 1 & 7, low[L_STATE]), 'features': None, 'images': None}
        if adv['instances'] in (0, 'reserved (11b)'):
            out['download'] = 'no CDB instance advertised (01h:163)'
            return out
        f = cdb_command(port, CMD['features'], say=say, selected=True)
        out['features'] = {**f.get('decoded', {}), 'supported': f['supported'] and f['ok'], 'status': _status(f)}
        i = cdb_command(port, CMD['info'], say=say, selected=True)
        out['images'] = {**i.get('decoded', {}), 'supported': i['supported'] and i['ok'], 'status': _status(i)}
    feat = out['features']
    mech = feat.get('write_mechanism')
    if not feat['supported'] or mech is None:
        out['download'] = 'not advertised: 0041h ' + feat['status']
    elif mech == 'none':
        out['download'] = 'none: WriteMechanism = none'
    else:
        out['download'] = f'yes: {mech}, header {feat.get("start_cmd_payload_size")} bytes'
    return out


def _identity(port):
    with port.selected():
        low = port.read(0, 128)
        p0 = port.read_page(0x00)
    d = decode_id(low, p0)
    return d.get('part_number', ''), d.get('vendor', ''), d.get('serial', '')


def _quiet(port):
    """ModuleLowPwr, or every Data Path deactivated: the only states a firmware run may start from."""
    with port.selected():
        st = port.read(L_STATE, 1)[0] >> 1 & 7
    if st == 1:
        return 'ModuleLowPwr'
    from .tests import dp_states
    codes = dp_states(port)
    if all(c == 1 for c in codes):
        return 'all Data Paths DPDeactivated'
    raise Refused(f'module state {STATES.get(st, st)} with Data Paths {[DP_STATES.get(c, c) for c in codes]}: '
                  'a firmware run needs ModuleLowPwr or every lane DPDeactivated (set LPMode, or deinit the lanes)')


def load_image(path):
    """The vendor file as bytes. Binary only: an Intel HEX file (':' lines) is refused with the reason (7.3.1.1: the
    host converts; the vendor says which encoding the module expects, this tool does not guess)."""
    with open(path, 'rb') as f:
        data = f.read()
    if not data:
        raise Refused(f'{path}: empty file')
    if re.match(rb':[0-9A-Fa-f]{10,}\r?\n', data):
        raise Refused(f'{path}: looks like Intel HEX; convert it to binary first (the vendor says which encoding the module expects)')
    return data


def _running(info):
    img = info.get('images') or {}
    for key in ('a', 'b'):
        if (img.get('bank_' + key) or {}).get('running') and img.get('image_' + key):
            return img['image_' + key]
    return None


def version_texts(image):
    """The texts a header may carry for a Table 9-15 version triple: '60.44', '60.44.0000', '96.68' (decimal)."""
    if not image:
        return set()
    bcd = image['bcd']
    return {bcd, bcd.rsplit('.', 1)[0], f"{image['major']}.{image['minor']}"}  # 'major.minor' alone counts: the stricter reading


def plan_update(info, image, part_number, header_bytes=None, image_version=None, force=False, commit=False, run=True):
    """Every guard that needs no module write, then the step list. Raises Refused on the first guard that fails."""
    feat = info.get('features') or {}
    mech = feat.get('write_mechanism')
    if not feat.get('supported') or mech in (None, 'none'):
        raise Refused(f"firmware download not advertised: {info.get('download')}")
    hdr = feat.get('start_cmd_payload_size') or 0
    if len(image) <= hdr:
        raise Refused(f'image {len(image)} bytes is not longer than the {hdr}-byte header the module asks for (0041h:138)')
    if len(image) > 0xFFFF_FFFF:
        raise Refused('image larger than the U32 ImageSize of Table 9-16')
    header = image[:hdr]
    pn = (part_number or '').strip().encode()
    pn_found = bool(pn) and pn in header
    if header_bytes is not None and header_bytes != hdr:
        raise Refused(f'--header-bytes {header_bytes} does not match the StartCmdPayloadSize {hdr} the module advertises')
    if not pn_found and header_bytes is None:
        raise Refused(f'the {hdr}-byte header does not carry the module part number {pn.decode() or "?"}; the vendor format is '
                      'unknown to the tool. Pass --header-bytes N (N = the advertised size) only when the vendor says this file '
                      'is for this module: a wrong file is the module\'s to reject, and a module that does not check may brick')
    running = _running(info)
    texts = version_texts(running)
    if image_version is not None:
        same = image_version in texts
    else:
        same = any(t.encode() in header for t in texts)
    if same and not force:
        raise Refused(f'the running image already reads {running["bcd"]}; --force to download it again')
    use_epl = mech in ('EPL', 'LPL and EPL') and bool((info.get('advert') or {}).get('epl_bytes'))
    ext = feat.get('rw_length_ext') or 0
    if use_epl:
        block = min(int(info['advert']['epl_bytes']), 8 * (1 + ext))
        chunk = min(8 * (1 + ext), 128)
    else:
        chunk = 8 * (1 + min(ext, 15))
        block = min(LPL_BLOCK, chunk * ((LPL_BLOCK + 4) // chunk) - 4) if chunk < LPL_BLOCK + 4 else LPL_BLOCK
    body = len(image) - hdr
    blocks = (body + block - 1) // block
    dur = {k: (feat.get('max_duration_ms') or {}).get(k) or DEFAULT_MS[k] for k in DEFAULT_MS}
    steps = [{'cmd': '0x0101', 'name': 'start', 'bytes': hdr, 'timeout_ms': dur['start']},
             {'cmd': '0x0104' if use_epl else '0x0103', 'name': 'write_epl' if use_epl else 'write_lpl', 'blocks': blocks,
              'block_bytes': block, 'timeout_ms': dur['write'], 'skip_erased': bool(feat.get('skipping_erased_blocks')),
              'erased_byte': feat.get('erased_byte')},
             {'cmd': '0x0107', 'name': 'complete', 'timeout_ms': dur['complete']},
             {'cmd': '0x0100', 'name': 'verify', 'expect': 'the inactive bank valid'}]
    if run:
        steps.append({'cmd': '0x0109', 'name': 'run', 'image_to_run': RUN_INACTIVE_RESET, 'delay_ms': RUN_DELAY_MS,
                      'wait_s': RUN_WAIT_S, 'hitless': bool(feat.get('hitless_restart'))})
    if commit:
        steps.append({'cmd': '0x010a', 'name': 'commit', 'needs': 'confirmation COMMIT <serial>'})
    steps.append({'on_failure': '0x0102 abort' if feat.get('abort_cmd') else '0x0107 complete (reads as a failed download)'})
    return {'image_bytes': len(image), 'header_bytes': hdr, 'part_number_in_header': pn_found,
            'running': running['bcd'] if running else None, 'mechanism': 'EPL' if use_epl else 'LPL',
            'block_bytes': block, 'blocks': blocks, 'chunk': chunk, 'steps': steps}


def _cmd(port, name, say, lpl=b'', epl=b'', timeout_ms=5000, chunk=8):
    return cdb_command(port, CMD[name], lpl, timeout=timeout_ms / 1000.0 + 0.5, say=say, raw=True, epl=epl, chunk=chunk, selected=True)


def _abort(port, say, feat):
    name = 'abort' if feat.get('abort_cmd') else 'complete'
    try:
        r = _cmd(port, name, say, timeout_ms=(feat.get('max_duration_ms') or {}).get(name) or DEFAULT_MS[name])
        return f'{name}: {_status(r)}'
    except (Nack, Refused) as e:
        return f'{name} failed: {e}'


def fw_update(port, image_path, say=None, dry_run=False, commit=False, run=True, force=False, header_bytes=None,
              confirm=None, image_version=None, password=None, run_only=False):
    """The reference procedure of 7.3.1.2 on one module end. Returns a dict with `plan`, `steps` (each with its CDB
    status) and `result`: PLANNED, UPDATED, RUNNING, COMMITTED, ABORTED, or a 'RUNNING, ...' / 'UPDATED, ...' note.
    The module is never released from low power."""
    say = _say(say)
    info = fw_info(port, say)
    pn, vendor, serial = _identity(port)
    image = load_image(image_path)
    plan = plan_update(info, image, pn, header_bytes, image_version, force or run_only, commit, run)  # run_only: the same version IS the point
    quiet = _quiet(port)
    bank_has = None  # run_only (FW-1 on the Credo): the image is already in a bank, skip the download
    if run_only:
        imgs = info.get('images') or {}
        want = image_version or plan.get('image_version')
        for k in ('a', 'b'):
            if want and str((imgs.get('image_' + k) or {}).get('bcd', '')).startswith(_pad_version(want)):
                bank_has = k.upper()
        if bank_has is None:
            raise Refused(f'--run-only: no bank holds image version {want}; download it first')
    if commit and confirm != f'COMMIT {serial}':
        raise Refused(f"commit needs the confirmation string 'COMMIT {serial}' (--confirm); a committed image runs on every reset")
    out = {'module': {'vendor': vendor, 'part_number': pn, 'serial': serial, 'quiet': quiet, 'running': plan['running']},
           'image': os.path.basename(image_path), 'plan': plan, 'steps': [], 'result': 'PLANNED'}
    for s in plan['steps']:
        say(f'plan: {s}')
    if dry_run:
        return out
    feat = info['features']
    hdr, block, chunk = plan['header_bytes'], plan['block_bytes'], plan['chunk']
    body = image[hdr:]
    erased = int(feat['erased_byte'], 16) if feat.get('erased_byte') else None
    inactive = 'A'
    running_bank = str((info.get('images') or {}).get('running', ''))[:1].upper()
    if run_only and bank_has == running_bank:  # the new image already runs: only a commit is left
        if not commit:
            say(f'bank {bank_has} already runs the image; nothing to do without --commit')
            out['result'] = 'RUNNING'
            return out
        run = False
    with port.selected() if not run_only else _nullcontext():
        try:
            if run_only:
                inactive = bank_has if bank_has != running_bank else inactive
                say(f'--run-only: bank {bank_has} holds the image, the download is skipped')
                raise _SkipDownload
            if password is not None:  # FW-1 on the Credo: 0101h answered 'password related error' (status 46h) with no entry
                port.write(PASSWORD_ENTRY, int(password).to_bytes(4, 'big'))  # Table 8-10: Password Entry Area, MSB first
                say(f'password entry written to 00h:{PASSWORD_ENTRY}-{PASSWORD_ENTRY + 3} (value not shown)')
                out['steps'].append({'name': 'password', 'status': 'written'})
            lpl = len(image).to_bytes(4, 'big') + bytes(4) + image[:hdr]  # Table 9-16: ImageSize, reserved, VendorData
            r = _cmd(port, 'start', say, lpl=lpl, timeout_ms=plan['steps'][0]['timeout_ms'], chunk=chunk)
            out['steps'].append({'name': 'start', 'status': _status(r), 'elapsed_s': r['elapsed_s']})
            if not r['ok']:
                raise Nack(f'start: {_status(r)}')
            n_sent = n_skipped = 0
            t0 = time.monotonic()
            for i in range(0, len(body), block):
                blk = body[i:i + block]
                if erased is not None and feat.get('skipping_erased_blocks') and set(blk) == {erased}:
                    n_skipped += 1
                    continue
                addr = i.to_bytes(4, 'big')
                if plan['mechanism'] == 'EPL':
                    r = _cmd(port, 'write_epl', say, lpl=addr, epl=blk, timeout_ms=plan['steps'][1]['timeout_ms'], chunk=chunk)
                else:
                    r = _cmd(port, 'write_lpl', say, lpl=addr + blk, timeout_ms=plan['steps'][1]['timeout_ms'], chunk=chunk)
                if not r['ok']:
                    out['steps'].append({'name': 'write', 'block': i // block, 'status': _status(r)})
                    raise Nack(f'write block {i // block} at {i}: {_status(r)}')
                n_sent += 1
                if n_sent % 16 == 0 or i + block >= len(body):
                    say(f'  write {i + len(blk)}/{len(body)} bytes, {n_sent} blocks sent, {n_skipped} erased blocks skipped, '
                        f'{time.monotonic() - t0:.1f} s')
            out['steps'].append({'name': 'write', 'blocks_sent': n_sent, 'blocks_skipped': n_skipped, 'bytes': len(body),
                                 'elapsed_s': round(time.monotonic() - t0, 2)})
            r = _cmd(port, 'complete', say, timeout_ms=plan['steps'][2]['timeout_ms'])
            out['steps'].append({'name': 'complete', 'status': _status(r), 'elapsed_s': r['elapsed_s']})
            if not r['ok']:
                raise Nack(f'complete: {_status(r)} (the module rejected the image: CRC or header)')
            r = _cmd(port, 'info', say)
            img = r.get('decoded') or {}
            inactive = 'B' if img.get('running') == 'A' else 'A'
            bank = img.get('bank_' + inactive.lower()) or {}
            out['steps'].append({'name': 'verify', 'firmware_status': img.get('firmware_status'), 'inactive_bank': inactive,
                                 'valid': bank.get('valid'), 'inactive_image': (img.get('image_' + inactive.lower()) or {}).get('bcd')})
            if not bank.get('valid'):
                raise Nack(f'verify: bank {inactive} not valid after Complete ({img.get("firmware_status")})')
            out['result'] = 'UPDATED'
        except _SkipDownload:
            pass
        except (Nack, Refused) as e:
            out['steps'].append({'name': 'abort', 'reason': str(e), 'status': _abort(port, say, feat)})
            out['result'] = 'ABORTED'
            say(f'ABORTED before Run: {e}; the committed image is untouched', err=True)
            return out
        if not run and not (run_only and bank_has == running_bank):  # commit-only: the image already runs, fall through
            say(f'bank {inactive} holds the new image, uncommitted and not running; `fw-update --run-image` starts it')
            return out
        if run:
            lpl = bytes([0, RUN_INACTIVE_RESET]) + RUN_DELAY_MS.to_bytes(2, 'big')  # Table 9-24
            with port.selected() if run_only else _nullcontext():
                if password is not None and run_only:
                    port.write(PASSWORD_ENTRY, int(password).to_bytes(4, 'big'))
                r = _cmd(port, 'run', say, lpl=lpl, timeout_ms=2000)
            out['steps'].append({'name': 'run', 'status': _status(r), 'elapsed_s': r['elapsed_s']})
            if not r['ok']:
                out['result'] = 'UPDATED, RUN REFUSED'
                say(f'Run refused: {_status(r)}; the new image sits in bank {inactive}, the old one keeps running', err=True)
                return out
    time.sleep(RUN_DELAY_MS / 1000.0)
    t0 = time.monotonic()
    while True:  # the module resets: it NACKs until it is back
        try:
            with port.selected():
                port.read(0, 1)
                r = _cmd(port, 'info', say)
            break
        except Nack:
            if time.monotonic() - t0 > RUN_WAIT_S:
                out['result'] = 'RUN, MODULE SILENT'
                say(f'the module did not answer within {RUN_WAIT_S:g} s after Run: power-cycle it; it boots the committed image', err=True)
                return out
            time.sleep(0.2)
    img = r.get('decoded') or {}
    rk = str(img.get('running', ''))[:1].lower()
    out['steps'].append({'name': 'after_run', 'firmware_status': img.get('firmware_status'), 'running': img.get('running'),
                         'back_after_s': round(time.monotonic() - t0, 1), 'running_image': (img.get('image_' + rk) or {}).get('bcd')})
    out['result'] = 'RUNNING'
    if not commit:
        say(f"running bank {img.get('running')} uncommitted: a reset returns to the committed image; "
            f"`fw-update --commit --confirm 'COMMIT {serial}'` keeps it")
        return out
    with port.selected():
        if password is not None:  # the Run reset cleared the entry area
            port.write(PASSWORD_ENTRY, int(password).to_bytes(4, 'big'))
        r = _cmd(port, 'commit', say, timeout_ms=5000)
        out['steps'].append({'name': 'commit', 'status': _status(r)})
        if not r['ok']:
            out['result'] = 'RUNNING, COMMIT REFUSED'
            return out
        img = (_cmd(port, 'info', say).get('decoded') or {})
        out['steps'].append({'name': 'after_commit', 'firmware_status': img.get('firmware_status'), 'committed': img.get('committed')})
        out['result'] = 'COMMITTED' if img.get('committed') == img.get('running') else 'RUNNING, COMMIT NOT TAKEN'
    return out


def fw_abort(port, say=None):
    """0102h when advertised, else 0107h: ends a download the module still holds open (7.3.1.2)."""
    say = _say(say)
    info = fw_info(port, say)
    feat = info.get('features') or {}
    if not feat.get('supported'):
        raise Refused(f"no firmware management features: {info.get('download')}")
    with port.selected():
        return {'result': _abort(port, say, feat)}


def cmd_fw(port, cmd, a, say, serial):
    """CLI: fw-info | fw-update --image FILE [--plan] [--run-image] [--commit --confirm 'COMMIT <serial>'] [--force]
    [--header-bytes N] [--image-version X.Y] [--password 0xNNNNNNNN] | fw-abort."""
    import json

    from .records import record_header, write_record
    if cmd == 'fw-info':
        res = fw_info(port, say)
        if a.json:
            say(json.dumps(res, indent=1))
        else:
            say(f"  active firmware {res['active_fw']}, inactive {res['inactive_fw']}, state {res['module_state']}; download: {res['download']}")
            for k, v in (res.get('features') or {}).items():
                say(f'  features.{k:<22} {v}')
            for k, v in (res.get('images') or {}).items():
                say(f'  images.{k:<24} {v}')
    elif cmd == 'fw-update':
        if not getattr(a, 'image', None):
            raise SystemExit('fw-update needs --image FILE (the vendor binary)')
        pw = int(a.password, 0) if getattr(a, 'password', None) else None
        if pw is not None and not 0 <= pw <= 0xFFFFFFFF:
            raise SystemExit('--password is four bytes, e.g. 0x12AB34CD')
        res = fw_update(port, a.image, say, dry_run=a.plan, commit=a.commit, run=a.run_image, force=a.force,
                        header_bytes=a.header_bytes, confirm=a.confirm, image_version=a.image_version, password=pw,
                        run_only=getattr(a, 'run_only', False))
        if a.json:
            say(json.dumps(res, indent=1))
        else:
            say(f"  {res['result']}: {len(res['steps'])} steps; plan {res['plan']['mechanism']} {res['plan']['blocks']} blocks of "
                f"{res['plan']['block_bytes']} bytes, header {res['plan']['header_bytes']}")
    else:
        res = fw_abort(port, say)
        say(f"  {res['result']}")
    if a.out:
        write_record({**record_header([serial]), cmd.replace('-', '_'): res}, a.out, f'{cmd}_{serial}_{{}}.json', say)
    return res
