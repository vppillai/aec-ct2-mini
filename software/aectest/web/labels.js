//   labelRows(obj) -> [[key, label, text]]: text is the formatted scalar, or null when the value is nested (app.js renders it).
//   A key mapped to null is a raw duplicate: hidden from the table, kept in the raw JSON.
'use strict';
const LABELS = {
  fru_manufacturer: ['Manufacturer'], fru_product: ['Product'], fru_serial: ['Unit serial'], fru_part: ['Board id'], fru_file_id: ['File id'], fru_rev: ['Board revision'], fru_order: ['Order number'], fru_case: ['Case'], fru_mfg_date: ['Build date'], fru_product_string: ['USB product string'], i2c_hz: ['I2C clock', v => `${v / 1000} kHz`], i2c_ramp: ['Clock ramp'],
  identifier_name: ['Identifier', (v, o) => `${v}${o.identifier ? ` (${o.identifier})` : ''}`], identifier: null,
  module_state: ['Module state'], module_state_raw: null, module_ctrl_byte26: null, module_flags_byte9: null,
  cmis_revision: ['CMIS', (v, o) => `${v}${o.memory_model ? `, ${o.memory_model} memory` : ''}`], memory_model: null,
  temp_c: ['Temperature', v => `${(+v).toFixed(1)} C`], vcc_v: ['Vcc', v => `${(+v).toFixed(3)} V`],
  power_class: ['Power class', (v, o) => `${v}${o.power_class_msa_max_w ? ` (MSA max ${o.power_class_msa_max_w} W)` : ''}`], power_class_msa_max_w: null,
  max_power_w: ['MaxPower', v => `${v} W`], media_interface_technology_00h_212: ['Media', v => String(v).replace(/^0x[0-9a-fA-F]+ /, '')],
  vendor: ['Vendor'], part_number: ['Part number'], revision: ['Revision'], serial: ['Serial'], date_code: ['Date code'],
  verdict: ['Verdict'], pattern: ['Pattern'], seconds: ['Seconds', v => `${v} s`], samples: ['Samples'], kind: ['Kind'], csv: ['CSV file'],
  lane_mask: ['Lane mask'], media_lane_mask: ['Media lane mask'], apsel: ['Application (ApSel)'], loopback_end: ['Loopback end'],
  snr_advertised_13h_130_5: ['SNR advertised'], module_ready_observed_s: ['Ready after', v => `${v} s`],
  media_lol_14h_139: ['LOL at the end'], media_lol_before_gate_14h_139: ['LOL before the gate'], media_ber: ['BER per lane'], media_snr_db: ['SNR per lane (dB)'],
  hex: ['Bytes (hex)'], page: ['Page'], off: ['Offset'], n: ['Count'], ms: ['Time', v => `${v} ms`], mgmt_init_ms: ['MgmtInit', v => `${v} ms`],
  module_present: ['Module present'], interrupt: ['Interrupt pending'], fru: ['FRU'], IntL: ['IntL'], ModPrsL: ['ModPrsL'],
  application: ['Application'], host: ['Host interface'], media: ['Media interface'], host_lanes: ['Host lanes'], media_lanes: ['Media lanes'], active: ['Active'],
  host_lane_assign: ['Host lane start mask'], data_path_id: ['DataPathID'], dp_states: ['Data path states'], flags: ['Flags'],
};
const human = k => String(k).replace(/_(00h|01h|02h|10h|11h|13h|14h|2Fh)_\d+(_\d+)?$/, '').replace(/_/g, ' ').replace(/^\w/, c => c.toUpperCase());
function labelRows(obj) {
  return Object.keys(obj).filter(k => LABELS[k] !== null).map(k => {
    const l = LABELS[k], v = obj[k];
    const scalar = v === null || v === undefined || typeof v !== 'object';
    const text = scalar ? (l && l[1] ? l[1](v, obj) : v === null || v === undefined ? 'none' : typeof v === 'boolean' ? (v ? 'yes' : 'no') : String(v)) : null;
    return [k, l ? l[0] : human(k), text];
  });
}
if (typeof module !== 'undefined') module.exports = { LABELS, human, labelRows };
