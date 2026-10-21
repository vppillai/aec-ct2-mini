"""aectest -- the AEC-CT2-MINI test software: the transport, the CMIS decoders, the tests, the technician flow, the web UI.

Every register offset in this package was read from the primary document, not from memory:
  [CMIS]  OIF CMIS 5.2 (oiforum.com) -- table numbers cited inline
  [MSA]   QSFP-DD hardware specification rev 7.1 -- Tables 7, 8, 9, 12
  [8024]  SFF-8024 rev 4.14 Table 4-1 (identifier codes)
  [FTDI]  the FT2232H datasheet (pin map, the MPSSE/UART default hazard)
  [pyftdi] pyftdi 0.57.2 source (i2c.py, ftdi.py) -- GPIO/I2C behaviour notes
"""
import os as _os
__version__ = open(_os.path.join(_os.path.dirname(__file__), 'VERSION')).read().strip()  # one source: VERSION (pyproject reads the same file)
HW_SUPPORT = {'AEC-CT2-MINI': ['0']}
