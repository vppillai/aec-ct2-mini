// field name the data carries. Pure: no DOM. app.js reads HELP for the bubbles, the Help page lists it as the glossary,
// and the Advanced result cards use it through kvTable. A later merge binds these to the `help` strings of the CMIS
// decoder package (claude-cmis5); keep the keys as field names so that binding is a map lookup.
// The interface names are the SFF-8024 code lists CMIS points at (Host Electrical Interface IDs; MM, SM, passive copper,
// active cable and BASE-T media IDs). The lists carry the common codes; an unknown code shows as its hex and the table name.
'use strict';
const HELP = {
  flags_at_read: 'The latched flags the cable read found and cleared. A module raises IntL at insert for them. They clear on read.',
  feature_matrix: 'Every capability the module advertises, in four states: works, hollow, not exercised, not advertised. Exercise runs the matching check.',
  i2c_clock: 'The I2C clock this unit talks to the module at. The unit opens at 100 kHz; after the cable is read, 20 clean stress rounds earn 400 kHz. A fallback means the module did not answer cleanly at the faster clock.',
  application: 'One advertised way to run this cable. It names a host side interface, a media side interface and the lane counts. Applications are numbered from 1.',
  host: 'The host side electrical interface (what the switch port speaks), as a CMIS code decoded to its name. 0x11 is 400GAUI-8 C2M: 400G over 8 lanes at 50G.',
  media: 'The media side interface (what runs on the cable or fiber), as a CMIS code decoded to its name from the table that matches the media type.',
  host_lanes: 'How many electrical lanes this application uses on the host side.',
  media_lanes: 'How many lanes this application uses on the media side (fibers or copper pairs).',
  active: 'Yes when the module runs this application now (ApSel in the active control set).',
  host_lane_assign: 'A bit mask of the host lanes an application may start on. Bit 0 is lane 1.',
  apsel: 'ApSel: the application number the tester selected for a run. The data path uses its lane counts.',
  data_path_id: 'DataPathID: lanes that share one data path carry the same id. The tester sets it to the lowest host lane of each lane group.',
  module_state: 'The module state machine: LowPwr (safe, under 1.5 W), Ready (working), Fault, or a transition state.',
  dp_states: 'The data path state per lane: Deactivated, Init, Activated, TxTurnOn and so on. Every lane of one application reads the same.',
  power_class: 'The CMIS power class 1 to 8. Each class has a maximum power; the module also states its own MaxPower.',
  max_power_w: 'The maximum power the module declares in its EEPROM. The tester refuses to release a module above the rail budget.',
  memory_model: 'Paged: the module has the upper pages 00h to 14h behind a page select. Flat: lower memory only.',
  cmis_revision: 'The CMIS version the module implements. 4.0 modules have no diagnostic pages 13h and 14h; 5.x modules can.',
  identifier_name: 'The form factor byte 00h:0. 0x18 is QSFP-DD.',
  media_interface_technology_00h_212: 'What drives the media: a VCSEL or laser type for optics, or a copper cable with or without equalizers.',
  temp_c: 'The module case temperature from its own sensor.',
  vcc_v: 'The supply voltage the module measures at its pins.',
  flags: 'Latched alarm and warning flags from page 11h: a bit set means the condition happened since the last read.',
  lane_mask: 'Which host lanes a run measures, as a hex mask. Bit 0 is lane 1.',
  media_lane_mask: 'Which media lanes a run measures. Read from the active application.',
  pattern: 'The PRBS test pattern, for example PRBS31Q. The module generates it on one end and checks it on the other.',
  media_ber: 'Bit error ratio per lane: errors divided by bits during the gate.',
  media_snr_db: 'Signal to noise ratio per lane in dB, as the module measures it. Some modules report 0 on every lane.',
  media_lol_14h_139: 'Loss of lock of the checker, per lane, read from 14h:139. The register clears on read.',
  mgmt_init_ms: 'MgmtInit: the time from power up to the module answering on the management bus.',
  module_ready_observed_s: 'How long the module took from low power to Ready after the tester released it.',
  'media generator': 'The lanes on which the module can generate a test pattern toward the media side.',
  'media checker': 'The lanes on which the module can check a test pattern coming from the media side.',
  'host generator': 'The lanes on which the module can generate a test pattern toward the host side.',
  loopbacks: 'The loopback paths the module supports: media side, host side, input or output.',
  'BER results': 'Yes when the module reports bit error counts, so a PRBS run can measure.',
  'SNR (media)': 'Yes when the module advertises an SNR measurement on the media side.',
  gating: 'Yes when the module can measure over a fixed gate time instead of free running.',
  'Diagnostic pages': 'Pages 13h and 14h: the pattern generators, checkers and loopbacks. Without them no PRBS and no loopback run.',
  CDB: 'Command Data Block: a command interface on page 9Fh for firmware queries and updates.',
  VDM: 'Versatile Diagnostics Monitoring: extra monitors on pages 20h to 2Fh. Some modules advertise it and report zeros.',
  thresholds: 'The alarm and warning limits the module carries for temperature, voltage, power and bias.',
  firmware: 'The firmware version the module reports, from CDB when it has one.',
  module_present: 'ModPrsL low: a module is seated in the cage.',
  interrupt: 'IntL low: the module raised a flag. Read the flags to see which.',
  // the unit's own FRU area (batch 7, item 12): the IPMI board info area in the FT2232H EEPROM
  fru_manufacturer: 'Who built the unit, from the FRU board area in the unit EEPROM.',
  fru_product: 'The product name the unit carries in its FRU area.',
  fru_serial: 'The unit serial, CT2M-NN, the same one the tool lists and the case label carries.',
  fru_part: 'The board id: the hash of the fabrication package the board was built from.',
  fru_file_id: 'The FRU file id: which layout of the area this is.',
  fru_rev: 'The board revision the unit was built as.',
  fru_order: 'The order number of the batch the board came from.',
  fru_case: 'The case set the unit sits in, when one is recorded.',
  fru_mfg_date: 'The build date the FRU area carries, from the minutes since 1996 field.',
  fru_product_string: 'The USB product string the FT2232H reports to the host.',
  fru_raw: 'The FRU board area bytes as the EEPROM holds them.',
  // CDB and firmware feature fields (batch 7, item 3): one line each
  supported_cmds: 'The CDB commands this module answers, by id and name. Grouped by family: status, features, firmware, monitoring, diagnostics, vendor.',
  max_completion_ms: 'The longest time the module needs to finish one CDB command, in milliseconds (0040h reply).',
  image_readback: 'Yes when the host can read a firmware image back out of the module (0041h bit 7).',
  skipping_erased_blocks: 'Yes when the host may skip a block that holds only the erased byte during a download.',
  copy_cmd: 'Yes when the module supports the copy command 0108h: copy the running image into the other bank.',
  abort_cmd: 'Yes when the module supports the abort command 0102h to end a download.',
  start_cmd_payload_size: 'StartCmdPayloadSize: how many vendor header bytes the start command 0101h carries. 0 means the whole file is the image.',
  erased_byte: 'The value an erased flash block holds. A skipped block is assumed to hold it.',
  rw_length_ext: 'RW length extension: how many bytes one I2C write may carry beyond 8. 255 allows the full 2048 byte block.',
  write_mechanism: 'How the host sends image blocks: LPL, EPL, or both. See the LPL and EPL lines.',
  read_mechanism: 'How the host reads an image back: LPL, EPL, both, or none.',
  hitless_restart: 'Yes when the module can start a new image without a traffic hit.',
  max_duration_ms: 'The longest time each firmware command may take: start, abort, write, complete, copy.',
  lpl: 'LPL, Local Payload: the 120 byte command block on page 9Fh bytes 136 to 255. Every CDB command carries one.',
  epl: 'EPL, Extended Payload: up to 2048 bytes over pages A0h to AFh, for firmware blocks.',
  rlpl: 'RLPL: the length of the reply local payload the module returns on page 9Fh.',
  rpl_length: 'The length of the reply payload the module returned, in bytes.',
  length: 'The length of the reply payload the module returned, in bytes.',
  status_text: 'The CDB engine state from the query 0000h: boot up, host password accepted, or module password accepted.',
  firmware_status: 'The firmware status byte of 0100h: one nibble per bank with running, committed and valid bits.',
  bank_a: 'Bank A: running when the module executes it, committed when it boots on reset, valid when its image checks.',
  bank_b: 'Bank B: running when the module executes it, committed when it boots on reset, valid when its image checks.',
  running: 'The bank the module executes now.',
  committed: 'The bank the module boots at the next reset.',
  image_a: 'The version in bank A as major.minor.build.',
  image_b: 'The version in bank B as major.minor.build.',
  image_information: 'The image information byte of 0100h: which banks carry a valid image.',
  active_fw: 'The firmware version the module runs now, from 00h bytes 39 and 40.',
  inactive_fw: 'The firmware version in the other bank.',
  download: 'Whether this module accepts a firmware download, and by which mechanism.',
  password: 'The vendor host password, four bytes, written to page 00h bytes 122 to 125 before the start command. The page never shows it again.',
  run_only: 'The bank already holds the version named in the image version box. Skip the download, go to run or commit.',
  image_version: 'The version the file carries, as X.Y. The verify step compares it with the bank after the download.',
  header_bytes: 'The vendor header size the start command carries. Give it only when the module advertises one.',
  plan: 'The CDB steps the engine will send, with their timeouts. Nothing is written.',
  steps: 'The CDB steps the engine sent, each with its status.',
  result: 'PLANNED, UPDATED, RUNNING, COMMITTED or ABORTED, with a note when a step was refused.',
};
// SFF-8024 Host Electrical Interface IDs (the CMIS HostInterfaceID byte)
const HOST_IDS = {
  0x01: '1000BASE-CX', 0x02: 'XAUI', 0x03: 'XFI', 0x04: 'SFI', 0x05: '25GAUI C2M', 0x06: 'XLAUI C2M', 0x07: 'XLPPI', 0x08: 'LAUI-2 C2M', 0x09: '50GAUI-2 C2M',
  0x0a: '50GAUI-1 C2M', 0x0b: 'CAUI-4 C2M', 0x0c: '100GAUI-4 C2M', 0x0d: '100GAUI-2 C2M', 0x0e: '200GAUI-8 C2M', 0x0f: '200GAUI-4 C2M', 0x10: '400GAUI-16 C2M', 0x11: '400GAUI-8 C2M',
  0x13: '10GBASE-CX4', 0x14: '25GBASE-CR CA-L', 0x15: '25GBASE-CR CA-S', 0x16: '25GBASE-CR CA-N', 0x17: '40GBASE-CR4', 0x18: '50GBASE-CR', 0x19: '100GBASE-CR10', 0x1a: '100GBASE-CR4',
  0x1b: '100GBASE-CR2', 0x1c: '200GBASE-CR4', 0x1d: '400G CR8', 0x1e: '1000BASE-T', 0x1f: '2.5GBASE-T', 0x20: '5GBASE-T', 0x21: '10GBASE-T', 0x22: '25GBASE-T', 0x23: '40GBASE-T', 0x24: '50GBASE-T',
  0x25: '8GFC', 0x26: '10GFC', 0x27: '16GFC', 0x28: '32GFC', 0x29: '64GFC', 0x2a: '128GFC', 0x2b: '256GFC', 0x2c: 'IB SDR', 0x2d: 'IB DDR', 0x2e: 'IB QDR', 0x2f: 'IB FDR', 0x30: 'IB EDR', 0x31: 'IB HDR', 0x32: 'IB NDR',
  0x41: '100GAUI-1-S C2M', 0x42: '100GAUI-1-L C2M', 0x43: '200GAUI-2-S C2M', 0x44: '200GAUI-2-L C2M', 0x45: '400GAUI-4-S C2M', 0x46: '400GAUI-4-L C2M', 0x47: '800G S C2M', 0x48: '800G L C2M',
  0x49: '100GBASE-CR1', 0x4a: '200GBASE-CR2', 0x4b: '400GBASE-CR4', 0x4c: '800GBASE-CR8',
};
// SFF-8024 media interface ID tables, chosen by the media type (CMIS 00h:85, or the technology byte 00h:212 when the passport has only that)
const MM_IDS = { 0x01: '10GBASE-SW', 0x02: '10GBASE-SR', 0x03: '25GBASE-SR', 0x04: '40GBASE-SR4', 0x05: '40GE SWDM4', 0x06: '40GE BiDi', 0x07: '50GBASE-SR', 0x08: '100GBASE-SR10', 0x09: '100GBASE-SR4', 0x0a: '100GE SWDM4', 0x0b: '100GE BiDi', 0x0c: '100GBASE-SR2', 0x0d: '100G-SR', 0x0e: '200GBASE-SR4', 0x0f: '400GBASE-SR16', 0x10: '400GBASE-SR8', 0x11: '400G-SR4', 0x12: '800G-SR8', 0x1a: '400GBASE-SR4.2', 0x1b: '8GFC-MM', 0x1c: '10GFC-MM', 0x1d: '16GFC-MM', 0x1e: '32GFC-MM', 0x1f: '64GFC-MM', 0x20: '128GFC-MM4', 0x21: '256GFC-MM4' };
const SM_IDS = { 0x01: '10GBASE-LW', 0x02: '10GBASE-EW', 0x03: '10G-ZW', 0x04: '10GBASE-LR', 0x05: '10GBASE-ER', 0x06: '10G-ZR', 0x07: '25GBASE-LR', 0x08: '25GBASE-ER', 0x09: '40GBASE-LR4', 0x0a: '40GBASE-FR', 0x0b: '50GBASE-FR', 0x0c: '50GBASE-LR', 0x0d: '100GBASE-LR4', 0x0e: '100GBASE-ER4', 0x0f: '100G PSM4', 0x10: '100G CWDM4', 0x11: '100G 4WDM-10', 0x12: '100G 4WDM-20', 0x13: '100G 4WDM-40', 0x14: '100GBASE-DR', 0x15: '100G-FR', 0x16: '100G-LR', 0x17: '200GBASE-DR4', 0x18: '200GBASE-FR4', 0x19: '200GBASE-LR4', 0x1a: '400GBASE-FR8', 0x1b: '400GBASE-LR8', 0x1c: '400GBASE-DR4', 0x1d: '400G-FR4', 0x1e: '400G-LR4-10', 0x3f: '400GBASE-LR4-6', 0x40: '100G-ZR', 0x41: '400G-ZR', 0x42: '800G-DR8', 0x43: '800G-DR8-2' };
const PC_IDS = { 0x01: 'Copper cable' };
const AC_IDS = { 0x01: 'Active cable, BER < 1e-12', 0x02: 'Active cable, BER < 5e-5', 0x03: 'Active cable, BER < 2.6e-4', 0x04: 'Active cable, BER < 1e-6', 0xbf: 'Active cable, BER < 1e-5' };
const BT_IDS = { 0x01: '1000BASE-T', 0x02: '2.5GBASE-T', 0x03: '5GBASE-T', 0x04: '10GBASE-T', 0x05: '25GBASE-T', 0x06: '40GBASE-T', 0x07: '50GBASE-T' };
const MEDIA_TABLES = { mm: ['MM optical', MM_IDS], sm: ['SM optical', SM_IDS], pc: ['passive copper', PC_IDS], ac: ['active cable', AC_IDS], bt: ['BASE-T', BT_IDS] };
// 00h:212 technology -> the media table: 0x00..0x01 and 0x0? VCSELs are MM, lasers are SM, 0x0a passive copper, 0x0b..0x0f active copper
function mediaKind(tech212) {
  const code = parseInt(String(tech212 || '').split(' ')[0], 16);
  if (Number.isNaN(code)) return null;
  if (code === 0x0a) return 'pc';
  if (code >= 0x0b && code <= 0x0f) return 'ac';
  if (code === 0x10 || code === 0x11) return 'bt';
  return code <= 0x01 ? 'mm' : 'sm';
}
function ifaceName(side, code, tech212) { // 'host' | 'media', the hex string or number -> 'name (0x11)' or '0x99 (table name)'
  const n = typeof code === 'number' ? code : parseInt(code, 16);
  const hex = `0x${(n || 0).toString(16).padStart(2, '0')}`;
  if (side === 'host') return HOST_IDS[n] ? `${HOST_IDS[n]} (${hex})` : `${hex} (host interface, unlisted)`;
  const kind = mediaKind(tech212), t = kind && MEDIA_TABLES[kind];
  return t && t[1][n] ? `${t[1][n]} (${hex})` : `${hex} (${t ? t[0] : 'media'} interface, unlisted)`;
}

// The CDB command ids and names (batch 7, item 1): the CMIS 5.2 section 9 command list. The features reply 0040h names the
// supported ids; this map turns '0x0101' into '0101h Start Firmware Download'. An unknown id reads as reserved or vendor.
const CDB_CMDS = {
  0x0000: 'Query Status', 0x0001: 'Enter Password', 0x0002: 'Change Password', 0x0003: 'Enable or Disable Password Protection', 0x0004: 'General Abort',
  0x0040: 'Module Features', 0x0041: 'Firmware Management Features', 0x0042: 'Performance Monitoring Features', 0x0043: 'BERT and Diagnostics Features',
  0x0044: 'Data Monitoring and Recording Features', 0x0050: 'Security Features',
  0x0100: 'Get Firmware Info', 0x0101: 'Start Firmware Download', 0x0102: 'Abort Firmware Download', 0x0103: 'Write Firmware Block LPL',
  0x0104: 'Write Firmware Block EPL', 0x0105: 'Read Firmware Block LPL', 0x0106: 'Read Firmware Block EPL', 0x0107: 'Complete Firmware Download',
  0x0108: 'Copy Firmware Image', 0x0109: 'Run Firmware Image', 0x010a: 'Commit Firmware Image',
  0x0200: 'Performance Monitoring Control', 0x0201: 'Get Performance Monitoring Bins', 0x0202: 'Get Performance Monitoring Thresholds',
  0x0380: 'Loopbacks', 0x0381: 'PRBS Generator Control', 0x0382: 'PRBS Checker Control', 0x0383: 'PRBS Checker Results',
};
const CDB_FAMILIES = [[0x0000, 0x003f, 'status and password'], [0x0040, 0x00ff, 'features'], [0x0100, 0x01ff, 'firmware'], [0x0200, 0x02ff, 'performance monitoring'],
  [0x0300, 0x03ff, 'BERT and diagnostics'], [0x0400, 0x7fff, 'reserved'], [0x8000, 0xffff, 'vendor']];
function cdbFamily(id) { const n = typeof id === 'number' ? id : parseInt(id, 16); return (CDB_FAMILIES.find(([a, b]) => n >= a && n <= b) || [0, 0, 'reserved'])[2]; }
function cdbName(id) { const n = typeof id === 'number' ? id : parseInt(id, 16); const hex = `${n.toString(16).padStart(4, '0')}h`; return `${hex} ${CDB_CMDS[n] || (cdbFamily(n) === 'vendor' ? 'vendor command' : 'reserved')}`; }
function cdbGroups(ids) { // ['0x0000', ...] -> [[family, ['0000h Query Status', ...]]], in family order
  const g = new Map(); for (const id of ids || []) { const f = cdbFamily(id); if (!g.has(f)) g.set(f, []); g.get(f).push(cdbName(id)); }
  return [...g.entries()];
}
if (typeof module !== 'undefined') module.exports = { HELP, HOST_IDS, MEDIA_TABLES, mediaKind, ifaceName, CDB_CMDS, cdbFamily, cdbName, cdbGroups };
