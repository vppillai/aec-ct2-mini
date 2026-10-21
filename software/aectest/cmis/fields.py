"""CMIS field maps per page: one row per field, data not code, so the decoder is one loop.

Row = F(byte, n, bits, name, kind, arg, unit, rev, adv):
  byte  first byte of the field (lower memory 0-127, upper pages 128-255)
  n     byte count (1, 2, 4, 8, 16 ...)
  bits  (hi, lo) inside a 1-byte field, or None for the whole bytes
  kind  'u' unsigned big endian, 's' signed, 'u16le' per-lane little endian words, 'ascii', 'hex', 'enum' (arg = code -> text),
        'bits' (arg = bit -> name; value = the names set), 'lanes' (one bit per lane 1-8, bit 0 = lane 1; value = [lane numbers set]),
        'nib' (two 4-bit lane fields per byte, low nibble = the odd lane; arg = enum or None; value = one entry per lane),
        'f16' (CMIS 3.4 floats per lane), 'scale' (arg = multiplier on the unsigned word), 'sscale' (signed), 'dur' (Table 8-43 code)
  unit  text for the reader
  rev   the lowest CmisRevision byte (00h:1) that defines the field: 0x30, 0x40, 0x50, 0x51, 0x52
  adv   the advertisement key the decoder resolves (see decode.ADV) or None = always present on a paged module

Sources: the OIF CMIS 5.2 tables, section 7 (line cites into the text extract). Rows marked
INFERENCE in their name carry a layout the extract did not confirm; the decoder keeps their raw bytes beside the value.
Revision deltas: 4.0 -> 5.0 moved the diagnostics pages 13h/14h to the layout used here (R6-W14), added the VDM observable
types 9-26 and Page 16h-19h; 5.1 added the 14h gated selectors 11h-15h; 5.2 added Page 15h latency and the 00h:211 far end
codes above 23; 5.3 is not covered: its fields are unverified.
"""
from collections import namedtuple

from . import DP_STATES, IDENTIFIERS, MEDIA_TECH, STATES

F = namedtuple('F', 'byte n bits name kind arg unit rev adv')


def f(byte, n, bits, name, kind, arg=None, unit='', rev=0x30, adv=None):
    return F(byte, n, bits, name, kind, arg, unit, rev, adv)


R40, R50, R51, R52 = 0x40, 0x50, 0x51, 0x52
LANE8 = 8

# ---- enums (the text of the spec table, not a judgement)
MEDIA_TYPES = {0: 'undefined', 1: 'optical MMF', 2: 'optical SMF', 3: 'passive copper', 4: 'active cable assembly',
               5: 'BASE-T'}  # Table 8-17
CONNECTORS = {0x00: 'undefined', 0x07: 'LC', 0x0B: 'optical pigtail', 0x0C: 'MPO 1x12', 0x0D: 'MPO 2x16', 0x21: 'copper pigtail',
              0x22: 'RJ45', 0x23: 'no separable connector', 0x24: 'MXC 2x16', 0x25: 'CS', 0x26: 'SN', 0x27: 'MPO 2x12',
              0x28: 'MPO 1x16'}  # SFF-8024 Table 4-3 (the codes a QSFP-DD cable or module uses)
FAULT_CAUSE = {0: 'no fault', 1: 'TEC runaway', 2: 'data memory corrupted', 3: 'program memory corrupted'}  # Table 8-16; 32-63 custom
CONFIG_STATUS = {0: 'ConfigUndefined', 1: 'ConfigSuccess', 2: 'ConfigRejected', 3: 'ConfigRejectedInvalidAppSel',
                 4: 'ConfigRejectedInvalidDataPath', 5: 'ConfigRejectedInvalidSI', 6: 'ConfigRejectedLanesInUse',
                 7: 'ConfigRejectedPartialDataPath', 12: 'ConfigInProgress'}  # Table 8-84; 8-11, 13-15 custom
DIAG_SELECTOR = {0x00: 'none', 0x01: 'BER host and media lanes (F16)', 0x02: 'host lanes 1-4 errors and bits', 0x03: 'host lanes 5-8 errors and bits',
                 0x04: 'media lanes 1-4 errors and bits', 0x05: 'media lanes 5-8 errors and bits', 0x06: 'SNR host and media lanes',
                 0x11: 'gated BER', 0x12: 'gated host lanes 1-4', 0x13: 'gated host lanes 5-8', 0x14: 'gated media lanes 1-4',
                 0x15: 'gated media lanes 5-8'}  # Table 8-119 (11h-15h from 5.1)
HOST_IDS = {0x11: '400GAUI-8 C2M (Annex 120E)'}  # SFF-8024 Table 4-5: only the code seen on the bench is named; others stay hex (audit gap G7)
LENGTH_MULT = {0: 0.1, 1: 1.0, 2: 10.0, 3: 100.0}  # Table 8-29 bits 7-6

# ---- lower memory (Table 8-4 l.6739; 8-5 l.6756; 8-6; 8-9 l.6912; 8-10 l.6971; 8-11 l.7009; 8-12; 8-13; 8-15; 8-16 l.7241; 8-18; 8-20; 8-22)
LOWER = [
    f(0, 1, None, 'SFF8024Identifier', 'enum', IDENTIFIERS),
    f(1, 1, None, 'CmisRevision', 'rev'),
    f(2, 1, (7, 7), 'MemoryModel', 'enum', {0: 'paged', 1: 'flat'}),
    f(2, 1, (6, 6), 'SteppedConfigOnly', 'u'),
    f(2, 1, (3, 2), 'MciMaxSpeed', 'enum', {0: '400 kHz', 1: '1 MHz'}, rev=R50),
    f(3, 1, (3, 1), 'ModuleState', 'enum', STATES),
    f(3, 1, (0, 0), 'InterruptDeasserted', 'u'),
    f(4, 4, None, 'FlagsSummary (banks 0-3, Table 8-8)', 'hex'),
    f(8, 1, None, 'ModuleFlags1', 'bits', {7: 'CdbCmdCompleteFlag2', 6: 'CdbCmdCompleteFlag1', 2: 'DataPathFirmwareErrorFlag',
                                            1: 'ModuleFirmwareErrorFlag', 0: 'ModuleStateChangedFlag'}),
    f(9, 1, None, 'ModuleFlags2 (Vcc / Temp)', 'bits', {7: 'VccMonLowWarning', 6: 'VccMonHighWarning', 5: 'VccMonLowAlarm', 4: 'VccMonHighAlarm',
                                                        3: 'TempMonLowWarning', 2: 'TempMonHighWarning', 1: 'TempMonLowAlarm', 0: 'TempMonHighAlarm'}),
    f(10, 1, None, 'ModuleFlags3 (Aux1 / Aux2)', 'bits', {7: 'Aux2LowWarning', 6: 'Aux2HighWarning', 5: 'Aux2LowAlarm', 4: 'Aux2HighAlarm',
                                                          3: 'Aux1LowWarning', 2: 'Aux1HighWarning', 1: 'Aux1LowAlarm', 0: 'Aux1HighAlarm'}),
    f(11, 1, None, 'ModuleFlags4 (Aux3 / Custom)', 'bits', {7: 'CustomLowWarning', 6: 'CustomHighWarning', 5: 'CustomLowAlarm', 4: 'CustomHighAlarm',
                                                            3: 'Aux3LowWarning', 2: 'Aux3HighWarning', 1: 'Aux3LowAlarm', 0: 'Aux3HighAlarm'}),
    f(14, 2, None, 'TempMonValue', 'sscale', 1 / 256, 'C', adv='mon.temp'),
    f(16, 2, None, 'VccMonVoltage', 'scale', 1e-4, 'V', adv='mon.vcc'),
    f(18, 2, None, 'Aux1MonValue', 's', None, 'raw S16', adv='mon.aux1'),
    f(20, 2, None, 'Aux2MonValue', 's', None, 'raw S16', adv='mon.aux2'),
    f(22, 2, None, 'Aux3MonValue', 's', None, 'raw S16', adv='mon.aux3'),
    f(24, 2, None, 'CustomMonValue', 'hex', adv='mon.custom'),
    f(26, 1, None, 'ModuleGlobalControls', 'bits', {7: 'BankBroadcastEnable', 6: 'LowPwrAllowRequestHW', 5: 'SquelchMethodSelect',
                                                     4: 'LowPwrRequestSW', 3: 'SoftwareReset'}),
    f(31, 1, None, 'ModuleMasks1', 'bits', {7: 'CdbCmdCompleteMask2', 6: 'CdbCmdCompleteMask1', 2: 'DataPathFirmwareErrorMask',
                                             1: 'ModuleFirmwareErrorMask', 0: 'ModuleStateChangedMask'}),
    f(32, 1, None, 'ModuleMasks2 (Vcc / Temp)', 'hex'),
    f(33, 1, None, 'ModuleMasks3 (Aux1 / Aux2)', 'hex'),
    f(34, 1, None, 'ModuleMasks4 (Aux3 / Custom)', 'hex'),
    f(37, 1, None, 'CdbStatus1', 'cdbstatus', adv='cdb'),
    f(38, 1, None, 'CdbStatus2', 'cdbstatus', adv='cdb2'),
    f(39, 2, None, 'ModuleActiveFirmware (major.minor)', 'ver'),
    f(41, 1, None, 'ModuleFaultCause', 'enum', FAULT_CAUSE),
    f(64, 21, None, 'Custom[21]', 'hex'),
    f(85, 1, None, 'MediaType', 'enum', MEDIA_TYPES),
    *[f(86 + 4 * i, 4, None, f'AppDescriptor{i + 1}', 'app') for i in range(8)],
    f(118, 4, None, 'PasswordEntry (WO)', 'hex'),
    f(122, 4, None, 'PasswordChange (WO)', 'hex'),
    f(126, 1, None, 'BankSelect', 'u'),
    f(127, 1, None, 'PageSelect', 'hex'),
]

# ---- Page 00h (Table 8-23 l.7499; 8-25; 8-26; 8-28; 8-29 l.7640; 8-30; 8-31; 8-32; 8-33; 8-35/8-36)
PAGE00 = [
    f(128, 1, None, 'SFF8024IdentifierCopy', 'enum', IDENTIFIERS),
    f(129, 16, None, 'VendorName', 'ascii'),
    f(145, 3, None, 'VendorOUI', 'hex'),
    f(148, 16, None, 'VendorPN', 'ascii'),
    f(164, 2, None, 'VendorRev', 'ascii'),
    f(166, 16, None, 'VendorSN', 'ascii'),
    f(182, 8, None, 'DateCode', 'ascii'),
    f(190, 10, None, 'CLEICode', 'ascii'),
    f(200, 1, (7, 5), 'ModulePowerClass (class = value + 1)', 'u'),
    f(201, 1, None, 'MaxPower', 'scale', 0.25, 'W'),
    f(202, 1, None, 'CableAssemblyLinkLength', 'length', None, 'm'),
    f(203, 1, None, 'ConnectorType', 'enum', CONNECTORS),
    f(204, 1, None, 'AttenuationAt5GHz', 'u', None, 'dB'),
    f(205, 1, None, 'AttenuationAt7GHz', 'u', None, 'dB'),
    f(206, 1, None, 'AttenuationAt12p9GHz', 'u', None, 'dB'),
    f(207, 1, None, 'AttenuationAt25p8GHz', 'u', None, 'dB'),
    f(210, 1, None, 'MediaLaneUnsupported (lanes set)', 'lanes'),
    f(211, 1, (4, 0), 'FarEndConfiguration (Table 8-34 code)', 'u'),
    f(212, 1, None, 'MediaInterfaceTechnology', 'enum', MEDIA_TECH),
    f(222, 1, None, 'PageChecksum (sum 128-221)', 'checksum', (128, 221)),
    f(223, 33, None, 'Custom[33]', 'hex'),
]

# ---- Page 01h (Table 8-37 l.7837; 8-38; 8-39; 8-40; 8-41; 8-42; 8-44 l.8072; 8-45 l.8155; 8-46 l.8193; 8-47 l.8215; 8-48 l.8253;
#      8-49; 8-51; 8-52 l.8484; 8-53)
PAGE01 = [
    f(128, 2, None, 'ModuleInactiveFirmware (major.minor)', 'ver'),
    f(130, 2, None, 'ModuleHardwareRevision (major.minor)', 'ver'),
    f(132, 1, None, 'SupportedLinkLengthSMF (km, bits 7-6 multiplier)', 'length', None, 'km'),
    f(133, 1, None, 'SupportedLinkLengthOM5', 'scale', 2, 'm'),
    f(134, 1, None, 'SupportedLinkLengthOM4', 'scale', 2, 'm'),
    f(135, 1, None, 'SupportedLinkLengthOM3', 'scale', 2, 'm'),
    f(136, 1, None, 'SupportedLinkLengthOM2', 'scale', 1, 'm'),
    f(138, 2, None, 'NominalWavelength', 'scale', 0.05, 'nm'),
    f(140, 2, None, 'WavelengthTolerance', 'scale', 0.005, 'nm'),
    f(142, 1, None, 'SupportedPages', 'bits', {6: 'VDMPagesSupported', 5: 'DiagnosticPagesSupported', 2: 'Page03hSupported'}),
    f(142, 1, (1, 0), 'BanksSupported (0 = bank 0, 1 = banks 0-1, 2 = banks 0-3)', 'u'),
    f(143, 1, None, 'ModSelWaitTime', 'modsel', None, 'us'),
    f(144, 1, (7, 4), 'MaxDurationDPDeinit', 'dur'),
    f(144, 1, (3, 0), 'MaxDurationDPInit', 'dur'),
    f(145, 1, (7, 7), 'CoolingImplemented', 'u'),
    f(145, 1, (3, 0), 'AuxMonObservableTypes (INFERENCE: bits as read)', 'u'),
    f(146, 1, None, 'ModuleTempMax', 's', None, 'C'),
    f(147, 1, None, 'ModuleTempMin', 's', None, 'C'),
    f(150, 1, None, 'OperatingVoltageMin', 'scale', 0.02, 'V'),
    f(151, 1, (7, 7), 'OpticalDetectorType (0 PIN, 1 APD)', 'u'),
    f(152, 1, None, 'CDRPowerSavedPerLane', 'scale', 0.01, 'W'),
    f(155, 1, None, 'SupportedControlsTx', 'bits', {7: 'WavelengthIsControllable', 6: 'TransmitterIsTunable', 3: 'ForcedSquelchTxSupported',
                                                     2: 'AutoSquelchDisableTxSupported', 1: 'OutputDisableTxSupported', 0: 'InputPolarityFlipTxSupported'}),
    f(155, 1, (5, 4), 'SquelchMethodTx (0 none, 1 OMA, 2 Pave, 3 either)', 'u'),
    f(156, 1, None, 'SupportedControlsRx', 'bits', {7: 'BankBroadcastSupported', 2: 'AutoSquelchDisableRxSupported', 1: 'OutputDisableRxSupported',
                                                     0: 'OutputPolarityFlipRxSupported'}),
    f(157, 1, None, 'SupportedFlagsTx', 'bits', {3: 'AdaptiveInputEqFailFlagTx', 2: 'CDRLOLFlagTx', 1: 'LOSFlagTx', 0: 'FailureFlagTx'}),
    f(158, 1, None, 'SupportedFlagsRx', 'bits', {2: 'CDRLOLFlagRx', 1: 'LOSFlagRx'}),
    f(159, 1, None, 'SupportedMonitorsModule', 'bits', {5: 'CustomMon', 4: 'Aux3Mon', 3: 'Aux2Mon', 2: 'Aux1Mon', 1: 'VccMon', 0: 'TempMon'}),
    f(160, 1, None, 'SupportedMonitorsLane', 'bits', {2: 'RxOpticalPowerMon', 1: 'TxOpticalPowerMon', 0: 'TxBiasMon'}),
    f(160, 1, (4, 3), 'TxBiasCurrentScalingFactor (0 x1, 1 x2, 2 x4)', 'u'),
    f(161, 1, None, 'SupportedSIControlsTx', 'bits', {4: 'TxInputEqFreeze', 3: 'TxInputAdaptiveEq', 2: 'TxInputEqFixedManual', 1: 'TxCDRBypassControl',
                                                       0: 'TxCDR'}),
    f(161, 1, (6, 5), 'TxInputEqRecallBuffersSupported', 'u'),
    f(162, 1, None, 'SupportedSIControlsRx', 'bits', {6: 'UnidirReconfig', 5: 'StagedSet1', 2: 'RxOutputAmplitudeControl', 1: 'RxCDRBypassControl',
                                                       0: 'RxCDR'}),
    f(162, 1, (4, 3), 'RxOutputEqControlSupported', 'u'),
    f(163, 1, (7, 6), 'CdbInstancesSupported', 'u'),
    f(163, 1, None, 'CdbOptions', 'bits', {5: 'CdbBackgroundMode', 4: 'CdbAutoPaging'}),
    f(163, 1, (3, 0), 'CdbMaxPagesEPL (Table 8-49 code)', 'u'),
    f(164, 1, None, 'CdbReadWriteLengthExtension', 'u'),
    f(167, 1, (7, 4), 'MaxDurationModulePwrDn', 'dur'),
    f(167, 1, (3, 0), 'MaxDurationModulePwrUp', 'dur'),
    f(168, 1, (7, 4), 'MaxDurationDPTxTurnOff', 'dur'),
    f(168, 1, (3, 0), 'MaxDurationDPTxTurnOn', 'dur'),
    *[f(176 + i, 1, None, f'MediaLaneAssignmentOptionsApp{i + 1} (lanes a Data Path may start on)', 'lanes') for i in range(15)],
    f(191, 32, None, 'Custom[32]', 'hex'),
    *[f(223 + 4 * i, 4, None, f'AppDescriptor{i + 9}', 'app') for i in range(7)],
    f(255, 1, None, 'PageChecksum (sum 130-254)', 'checksum', (130, 254)),
]

# ---- Page 02h (Table 8-54; 8-55; 8-56)
PAGE02 = [
    f(128, 2, None, 'TempMonHighAlarm', 'sscale', 1 / 256, 'C'), f(130, 2, None, 'TempMonLowAlarm', 'sscale', 1 / 256, 'C'),
    f(132, 2, None, 'TempMonHighWarning', 'sscale', 1 / 256, 'C'), f(134, 2, None, 'TempMonLowWarning', 'sscale', 1 / 256, 'C'),
    f(136, 2, None, 'VccMonHighAlarm', 'scale', 1e-4, 'V'), f(138, 2, None, 'VccMonLowAlarm', 'scale', 1e-4, 'V'),
    f(140, 2, None, 'VccMonHighWarning', 'scale', 1e-4, 'V'), f(142, 2, None, 'VccMonLowWarning', 'scale', 1e-4, 'V'),
    f(144, 8, None, 'Aux1MonThresholds (raw, unit per 01h:145)', 'hex', adv='mon.aux1'),
    f(152, 8, None, 'Aux2MonThresholds (raw)', 'hex', adv='mon.aux2'),
    f(160, 8, None, 'Aux3MonThresholds (raw)', 'hex', adv='mon.aux3'),
    f(168, 8, None, 'CustomMonThresholds (raw)', 'hex', adv='mon.custom'),
    f(176, 2, None, 'OpticalPowerTxHighAlarm', 'scale', 1e-4, 'mW', adv='mon.tx_power'), f(178, 2, None, 'OpticalPowerTxLowAlarm', 'scale', 1e-4, 'mW', adv='mon.tx_power'),
    f(180, 2, None, 'OpticalPowerTxHighWarning', 'scale', 1e-4, 'mW', adv='mon.tx_power'), f(182, 2, None, 'OpticalPowerTxLowWarning', 'scale', 1e-4, 'mW', adv='mon.tx_power'),
    f(184, 2, None, 'LaserBiasHighAlarm', 'bias', None, 'mA', adv='mon.tx_bias'), f(186, 2, None, 'LaserBiasLowAlarm', 'bias', None, 'mA', adv='mon.tx_bias'),
    f(188, 2, None, 'LaserBiasHighWarning', 'bias', None, 'mA', adv='mon.tx_bias'), f(190, 2, None, 'LaserBiasLowWarning', 'bias', None, 'mA', adv='mon.tx_bias'),
    f(192, 2, None, 'OpticalPowerRxHighAlarm', 'scale', 1e-4, 'mW', adv='mon.rx_power'), f(194, 2, None, 'OpticalPowerRxLowAlarm', 'scale', 1e-4, 'mW', adv='mon.rx_power'),
    f(196, 2, None, 'OpticalPowerRxHighWarning', 'scale', 1e-4, 'mW', adv='mon.rx_power'), f(198, 2, None, 'OpticalPowerRxLowWarning', 'scale', 1e-4, 'mW', adv='mon.rx_power'),
    f(255, 1, None, 'PageChecksum (sum 128-254)', 'checksum', (128, 254)),
]

PAGE03 = [f(128, 128, None, 'UserEEPROM', 'hex')]  # Table 8-57, advertised 01h:142.2
PAGE04 = [f(128, 128, None, 'TunableLaserCapabilities (raw, Table 8-59)', 'hex')]  # advertised 01h:155.6

# ---- Page 10h (Table 8-60 l.8832; 8-61; 8-62 l.8961; 8-63 l.9104; 8-64/8-65 l.9173; 8-66 l.9215; 8-67 l.9258; 8-68; 8-74)
_LANE_CTRL = [(129, 'InputPolarityFlipTx', 'ctl.polarity_tx'), (130, 'OutputDisableTx', 'ctl.disable_tx'), (131, 'AutoSquelchDisableTx', 'ctl.autosquelch_tx'),
              (132, 'OutputSquelchForceTx', 'ctl.force_squelch_tx'), (134, 'AdaptiveInputEqFreezeTx', 'si.eq_freeze'),
              (137, 'OutputPolarityFlipRx', 'ctl.polarity_rx'), (138, 'OutputDisableRx', 'ctl.disable_rx'), (139, 'AutoSquelchDisableRx', 'ctl.autosquelch_rx')]
PAGE10 = [
    f(128, 1, None, 'DPDeinit (lanes set = deinitialised)', 'lanes'),
    *[f(b, 1, None, n + ' (lanes set)', 'lanes', adv=a) for b, n, a in _LANE_CTRL],
    f(135, 2, None, 'AdaptiveInputEqStoreTx (WO, 2 bits per lane)', 'hex', adv='si.eq_recall'),
    f(143, 1, None, 'SCS0 ApplyDPInit (WO)', 'lanes'),
    f(144, 1, None, 'SCS0 ApplyImmediate (WO)', 'lanes'),
    *[f(145 + i, 1, None, f'SCS0 DPConfigLane{i + 1}', 'dpconfig') for i in range(8)],
    f(153, 1, None, 'SCS0 AdaptiveInputEqEnableTx (lanes set)', 'lanes', adv='si.adaptive_eq'),
    f(154, 2, None, 'SCS0 AdaptiveInputEqRecallTx (2 bits per lane)', 'hex', adv='si.eq_recall'),
    f(156, 4, None, 'SCS0 FixedInputEqTargetTx (nibble per lane)', 'nib', adv='si.fixed_eq'),
    f(160, 1, None, 'SCS0 CDREnableTx (lanes set)', 'lanes', adv='si.cdr_tx'),
    f(161, 1, None, 'SCS0 CDREnableRx (lanes set)', 'lanes', adv='si.cdr_rx'),
    f(162, 4, None, 'SCS0 OutputEqPreCursorTargetRx (nibble per lane)', 'nib', adv='si.rx_eq'),
    f(166, 4, None, 'SCS0 OutputEqPostCursorTargetRx (nibble per lane)', 'nib', adv='si.rx_eq'),
    f(170, 4, None, 'SCS0 OutputAmplitudeTargetRx (nibble per lane)', 'nib', adv='si.rx_amp'),
    f(176, 1, None, 'SCS0 ApplyImmediateTx (WO)', 'lanes', adv='si.unidir'),
    f(177, 1, None, 'SCS0 ApplyImmediateRx (WO)', 'lanes', adv='si.unidir'),
    f(178, 35, None, 'SCS1 (raw, same layout as 143-177)', 'hex', adv='si.scs1'),
    f(213, 1, None, 'DPStateChangedMask', 'lanes'),
    f(214, 1, None, 'FailureFlagTxMask', 'lanes'), f(215, 1, None, 'LOSFlagTxMask', 'lanes'), f(216, 1, None, 'CDRLOLFlagTxMask', 'lanes'),
    f(217, 1, None, 'AdaptiveInputEqFailFlagTxMask', 'lanes'),
    f(218, 8, None, 'TxPowerAndBiasAlarmWarningMasks (raw)', 'hex'),
    f(226, 1, None, 'LOSFlagRxMask', 'lanes'), f(227, 1, None, 'CDRLOLFlagRxMask', 'lanes'),
    f(228, 4, None, 'RxPowerAlarmWarningMasks (raw)', 'hex'),
    f(240, 16, None, 'Custom[16]', 'hex'),
]

# ---- Page 11h (Table 8-75 l.9705; 8-76/8-77; 8-78 l.9779; 8-79 l.9813; 8-80 l.9826; 8-81 l.9931; 8-82 l.10032; 8-83 l.10105; 8-84 l.10119;
#      8-85/8-86 l.10195; 8-87 l.10238; 8-88; 8-89 l.10332; 8-90)
PAGE11 = [
    f(128, 4, None, 'DPStateHostLane1-8', 'nib', DP_STATES),
    f(132, 1, None, 'OutputStatusRx (lanes valid)', 'lanes'),
    f(133, 1, None, 'OutputStatusTx (lanes valid)', 'lanes'),
    f(134, 1, None, 'DPStateChangedFlag (latched, lanes set)', 'lanes'),
    f(135, 1, None, 'FailureFlagTx (latched, media lanes set)', 'lanes', adv='flag.failure_tx'),
    f(136, 1, None, 'LOSFlagTx (latched, host lanes set)', 'lanes', adv='flag.los_tx'),
    f(137, 1, None, 'CDRLOLFlagTx (latched, host lanes set)', 'lanes', adv='flag.lol_tx'),
    f(138, 1, None, 'AdaptiveInputEqFailFlagTx (latched)', 'lanes', adv='flag.eq_fail_tx'),
    f(139, 1, None, 'OpticalPowerHighAlarmFlagTx', 'lanes', adv='mon.tx_power'), f(140, 1, None, 'OpticalPowerLowAlarmFlagTx', 'lanes', adv='mon.tx_power'),
    f(141, 1, None, 'OpticalPowerHighWarningFlagTx', 'lanes', adv='mon.tx_power'), f(142, 1, None, 'OpticalPowerLowWarningFlagTx', 'lanes', adv='mon.tx_power'),
    f(143, 1, None, 'LaserBiasHighAlarmFlagTx', 'lanes', adv='mon.tx_bias'), f(144, 1, None, 'LaserBiasLowAlarmFlagTx', 'lanes', adv='mon.tx_bias'),
    f(145, 1, None, 'LaserBiasHighWarningFlagTx', 'lanes', adv='mon.tx_bias'), f(146, 1, None, 'LaserBiasLowWarningFlagTx', 'lanes', adv='mon.tx_bias'),
    f(147, 1, None, 'LOSFlagRx (latched, media lanes set)', 'lanes', adv='flag.los_rx'),
    f(148, 1, None, 'CDRLOLFlagRx (latched, media lanes set)', 'lanes', adv='flag.lol_rx'),
    f(149, 1, None, 'OpticalPowerHighAlarmFlagRx', 'lanes', adv='mon.rx_power'), f(150, 1, None, 'OpticalPowerLowAlarmFlagRx', 'lanes', adv='mon.rx_power'),
    f(151, 1, None, 'OpticalPowerHighWarningFlagRx', 'lanes', adv='mon.rx_power'), f(152, 1, None, 'OpticalPowerLowWarningFlagRx', 'lanes', adv='mon.rx_power'),
    f(154, 16, None, 'OpticalPowerTx1-8', 'u16x8', 1e-4, 'mW', adv='mon.tx_power'),
    f(170, 16, None, 'LaserBiasTx1-8', 'biasx8', None, 'mA', adv='mon.tx_bias'),
    f(186, 16, None, 'OpticalPowerRx1-8', 'u16x8', 1e-4, 'mW', adv='mon.rx_power'),
    f(202, 4, None, 'ConfigStatusLane1-8', 'nib', CONFIG_STATUS),
    *[f(206 + i, 1, None, f'ACS DPConfigLane{i + 1}', 'dpconfig') for i in range(8)],
    f(214, 1, None, 'ACS AdaptiveInputEqEnableTx (lanes set)', 'lanes', adv='si.adaptive_eq'),
    f(215, 2, None, 'ACS AdaptiveInputEqRecalledTx (2 bits per lane)', 'hex', adv='si.eq_recall'),
    f(217, 4, None, 'ACS FixedInputEqTargetTx (nibble per lane)', 'nib', adv='si.fixed_eq'),
    f(221, 1, None, 'ACS CDREnableTx (lanes set)', 'lanes', adv='si.cdr_tx'),
    f(222, 1, None, 'ACS CDREnableRx (lanes set)', 'lanes', adv='si.cdr_rx'),
    f(223, 4, None, 'ACS OutputEqPreCursorTargetRx (nibble per lane)', 'nib', adv='si.rx_eq'),
    f(227, 4, None, 'ACS OutputEqPostCursorTargetRx (nibble per lane)', 'nib', adv='si.rx_eq'),
    f(231, 4, None, 'ACS OutputAmplitudeTargetRx (nibble per lane)', 'nib', adv='si.rx_amp'),
    f(235, 1, None, 'DPInitPending (lanes set)', 'lanes'),
    f(240, 8, None, 'MediaLaneToWavelengthAndFiberMappingTx1-8 (nibbles: wavelength, fiber)', 'hex'),
    f(248, 8, None, 'MediaLaneToWavelengthAndFiberMappingRx1-8 (nibbles: wavelength, fiber)', 'hex'),
]

PAGE12 = [f(128, 128, None, 'TunableLaserStatusAndFlags (raw, Table 8-92)', 'hex')]  # advertised 01h:155.6

# ---- Page 13h (Table 8-93 l.10550; 8-94 l.10615; 8-95 l.10631; 8-96 l.10675; 8-97 l.10749; 8-99 l.10794; 8-100; 8-101 l.10876;
#      8-102..8-109 l.10941; 8-110 l.11175; 8-114 l.11487; 8-115 l.11533; 8-116)
_PAT_CAP = [(132, 'HostSideGeneratorSupportsPattern'), (134, 'MediaSideGeneratorSupportsPattern'), (136, 'HostSideCheckerSupportsPattern'),
            (138, 'MediaSideCheckerSupportsPattern')]
_GEN = [(144, 'HostSideGenerator', 'gen_host'), (152, 'MediaSideGenerator', 'gen_media'), (160, 'HostSideChecker', 'chk_host'),
        (168, 'MediaSideChecker', 'chk_media')]
PAGE13 = [
    f(128, 1, None, 'LoopbackCapabilities', 'bits', {6: 'SimultaneousHostAndMediaSideLoopbacks', 5: 'PerLaneMediaSideLoopbacks', 4: 'PerLaneHostSideLoopbacks',
                                                      3: 'HostSideInputLoopback', 2: 'HostSideOutputLoopback', 1: 'MediaSideInputLoopback',
                                                      0: 'MediaSideOutputLoopback'}),
    f(129, 1, (7, 6), 'GatingSupport (0 none, 1 single timer, 2 per lane)', 'u'),
    f(129, 1, None, 'DiagnosticsMeasurementCapabilities', 'bits', {5: 'GatingResultsSupported', 4: 'PeriodicUpdatesSupported', 3: 'PerLaneGatingTimersSupported',
                                                                   2: 'AutoRestartGatingSupported'}),
    f(130, 1, None, 'DiagnosticReportingCapabilities', 'bits', {7: 'MediaSideFEC', 6: 'HostSideFEC', 5: 'MediaSideInputSNRMeasurement', 4: 'HostSideInputSNRMeasurement',
                                                                1: 'BitsAndErrorsCountingSupported', 0: 'BitErrorRatioResultsSupported'}),
    f(131, 1, None, 'PatternGenerationAndCheckingLocation', 'bits', {7: 'PRBSGeneratorMediaSidePreFEC', 6: 'PRBSGeneratorMediaSidePostFEC',
                                                                     5: 'PRBSCheckerMediaSidePreFEC', 4: 'PRBSCheckerMediaSidePostFEC',
                                                                     3: 'PRBSGeneratorHostSidePreFEC', 2: 'PRBSGeneratorHostSidePostFEC',
                                                                     1: 'PRBSCheckerHostSidePreFEC', 0: 'PRBSCheckerHostSidePostFEC'}),
    *[f(b, 2, None, n + '0-15 (Table 8-98 ids set)', 'patterns') for b, n in _PAT_CAP],
    f(140, 1, (7, 6), 'RecoveredClockForGeneratorOptions', 'u'),
    f(140, 1, (5, 5), 'ReferenceClockForPatternsSupported', 'u'),
    f(140, 1, (3, 0), 'UserPatternLengthSupported', 'u'),
    f(141, 1, None, 'SwapAndInvertCapabilities', 'bits', {7: 'MediaCheckerDataSwap', 6: 'MediaCheckerDataInvert', 5: 'MediaGeneratorDataSwap', 4: 'MediaGeneratorDataInvert',
                                                          3: 'HostCheckerDataSwap', 2: 'HostCheckerDataInvert', 1: 'HostGeneratorDataSwap', 0: 'HostGeneratorDataInvert'}),
    f(142, 1, None, 'PerLaneCapabilities', 'bits', {7: 'MediaCheckerPerLaneEnable', 6: 'MediaCheckerPerLanePattern', 5: 'MediaGeneratorPerLaneEnable',
                                                    4: 'MediaGeneratorPerLanePattern', 3: 'HostCheckerPerLaneEnable', 2: 'HostCheckerPerLanePattern',
                                                    1: 'HostGeneratorPerLaneEnable', 0: 'HostGeneratorPerLanePattern'}),
    *[row for b, n, a in _GEN for row in (
        f(b, 1, None, n + 'Enable (lanes set)', 'lanes', adv='diag.' + a), f(b + 1, 1, None, n + 'DataInvert (lanes set)', 'lanes', adv='diag.' + a),
        f(b + 2, 1, None, n + 'ByteSwap (lanes set)', 'lanes', adv='diag.' + a),
        f(b + 3, 1, None, n + ('PreFECEnable' if 'Generator' in n else 'PostFECEnable') + ' (lanes set)', 'lanes', adv='diag.' + a),
        f(b + 4, 4, None, n + 'PatternSelect (Table 8-98 id per lane)', 'nib', adv='diag.' + a))],
    f(176, 1, (7, 4), 'HostPRBSGeneratorClockSource', 'u'),
    f(176, 1, (3, 0), 'MediaPRBSGeneratorClockSource', 'u'),
    f(177, 1, None, 'ClockingAndMeasurementControls', 'bits', {7: 'StartStopIsGlobal', 5: 'ResetErrorInformation', 4: 'AutoRestartGating'}),
    f(177, 1, (3, 0), 'GateTimerSelect (INFERENCE: bits as read)', 'u'),
    f(178, 2, None, 'GateTimers (raw, Table 8-110)', 'hex'),
    f(180, 1, None, 'MediaSideOutputLoopbackEnable (lanes set)', 'lanes', adv='loop.media_out'),
    f(181, 1, None, 'MediaSideInputLoopbackEnable (lanes set)', 'lanes', adv='loop.media_in'),
    f(182, 1, None, 'HostSideOutputLoopbackEnable (lanes set)', 'lanes', adv='loop.host_out'),
    f(183, 1, None, 'HostSideInputLoopbackEnable (lanes set)', 'lanes', adv='loop.host_in'),
    f(196, 10, None, 'Custom[10]', 'hex'),
    f(206, 1, (7, 7), 'LossOfReferenceClockMask', 'u'),
    f(208, 1, None, 'PatternCheckGatingCompleteMaskHost', 'lanes'), f(209, 1, None, 'PatternCheckGatingCompleteMaskMedia', 'lanes'),
    f(210, 1, None, 'PatternGeneratorLOLMaskHost', 'lanes'), f(211, 1, None, 'PatternGeneratorLOLMaskMedia', 'lanes'),
    f(212, 1, None, 'PatternCheckerLOLMaskHost', 'lanes'), f(213, 1, None, 'PatternCheckerLOLMaskMedia', 'lanes'),
    f(224, 32, None, 'UserPattern', 'hex'),
]

# ---- Page 14h (Table 8-117 l.11615; 8-118; 8-119 l.11650; 8-120 l.11683; 8-121 l.11763)
PAGE14 = [
    f(128, 1, None, 'DiagnosticsSelector', 'enum', DIAG_SELECTOR),
    f(130, 2, None, 'Custom[2]', 'hex'),
    f(132, 1, (7, 7), 'LossOfReferenceClockFlag (latched)', 'u'),
    f(134, 1, None, 'PatternCheckGatingCompleteFlagHost (latched, lanes set)', 'lanes'),
    f(135, 1, None, 'PatternCheckGatingCompleteFlagMedia (latched, lanes set)', 'lanes'),
    f(136, 1, None, 'PatternGeneratorLOLFlagHost (latched, lanes set)', 'lanes'),
    f(137, 1, None, 'PatternGeneratorLOLFlagMedia (latched, lanes set)', 'lanes'),
    f(138, 1, None, 'PatternCheckerLOLFlagHost (latched, lanes set)', 'lanes'),
    f(139, 1, None, 'PatternCheckerLOLFlagMedia (latched, lanes set)', 'lanes'),
    f(192, 64, None, 'DiagnosticsData (decoded by the selector)', 'diagdata'),
]

PAGE2F = [  # Table 8-159 l.13184
    f(128, 1, (1, 0), 'VDMSupport (groups - 1)', 'u'),
    f(129, 2, None, 'FineIntervalLength', 'scale', 0.1, 'ms'),
    f(144, 1, (7, 7), 'FreezeRequest', 'u'),
    f(145, 1, None, 'FreezeStatus', 'bits', {7: 'FreezeDone', 6: 'UnfreezeDone'}),
]

PAGE9F = [  # Table 8-160 / 8-161 / 8-162
    f(128, 2, None, 'CMDID (last command written)', 'hex'),
    f(130, 2, None, 'EPLLength', 'u'),
    f(132, 1, None, 'LPLLength', 'u'),
    f(133, 1, None, 'CdbChkCode', 'hex'),
    f(134, 1, None, 'RPLLength', 'u'),
    f(135, 1, None, 'RPLChkCode', 'hex'),
    f(136, 120, None, 'LPL (last reply or command body)', 'hex'),
]

# ---- the page table: key as the dump names it -> (title, field rows, gate key or None, lowest revision)
PAGES = {
    'lower': ('Lower memory', LOWER, None, 0x30),
    '00h': ('Page 00h: identity', PAGE00, None, 0x30),
    '01h': ('Page 01h: advertisements', PAGE01, 'paged', 0x30),
    '02h': ('Page 02h: thresholds', PAGE02, 'paged', 0x30),
    '03h': ('Page 03h: user EEPROM', PAGE03, 'page03', 0x30),
    '04h': ('Page 04h: tunable laser capabilities', PAGE04, 'tunable', 0x30),
    '10h': ('Page 10h: lane controls', PAGE10, 'paged', 0x30),
    '11h': ('Page 11h: lane status', PAGE11, 'paged', 0x30),
    '12h': ('Page 12h: tunable laser status', PAGE12, 'tunable', 0x30),
    '13h': ('Page 13h: diagnostics capabilities and controls', PAGE13, 'diag', R50),
    '14h': ('Page 14h: diagnostics flags and data', PAGE14, 'diag', R50),
    '20h': ('Page 20h-23h: VDM descriptors', [], 'vdm', R40),
    '24h': ('Page 24h-27h: VDM samples', [], 'vdm', R40),
    '28h': ('Page 28h-2Bh: VDM thresholds (raw, Table 8-155)', [f(128, 128, None, 'VDMThresholds (raw)', 'hex')], 'vdm', R40),
    '2Ch': ('Page 2Ch: VDM threshold crossing flags (raw, Table 8-156)', [f(128, 128, None, 'VDMFlags (raw)', 'hex')], 'vdm', R40),
    '2Dh': ('Page 2Dh: VDM masks (raw, Table 8-158)', [f(128, 128, None, 'VDMMasks (raw)', 'hex')], 'vdm', R40),
    '2Fh': ('Page 2Fh: VDM advertisement and control', PAGE2F, 'vdm', R40),
    '9Fh': ('Page 9Fh: CDB message', PAGE9F, 'cdb', R40),
}
UNREAD_PAGES = {  # pages the stack names but does not read: no verified gate bit in the extract (audit gap G5)
    '15h': 'Data Path latency (Table 8-122, 5.2)', '16h': 'Network Path provisioning (Table 8-124, 5.0)', '17h': 'Network Path flags (Table 8-144)',
    '19h': 'Active Control Set per direction (Table 8-147)', 'A0h-AFh': 'CDB EPL segments (Table 8-164), 01h:163.3-0 pages',
}
