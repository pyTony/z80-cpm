# TODO / ideas

* Idea: use `z80sim.Z80` as BBC-style Z80 machine code / "second processor" support for
  the mini_basic project (`CALL`/`USR` with register variables, OS calls as trap
  addresses, optional small Z80 assembler). Few programs exist for that setup, so
  only an idea for now.
* Faster core: inline the remaining DD/FD (IXH/IXL, DDCB) and ED (RRD/RLD, IN/OUT) opcodes, merge common instruction pairs.
* Run the ZEXDOC/ZEXALL exerciser to completion (hours in pure Python).
* Windows keyboard/VT handling has had little testing.
