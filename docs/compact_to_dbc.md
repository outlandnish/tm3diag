# compact_to_dbc.py — Convert compact JSON to DBC

Converts `Model3_ETH.compact.json` to a standard DBC file for use in tools like CANdb++, Vector CANalyzer, Cangaroo, or SavvyCAN.

```
python compact_to_dbc.py                          # data/Model3_ETH.compact.json -> Model3_ETH.dbc
python compact_to_dbc.py input.json output.dbc
```

The output DBC includes:

- All messages (`BO_`) with correct ID, length, and sender
- All signals (`SG_`) with start bit, width, byte order, sign, scale, offset, min/max, units, and receivers
- Multiplexed signals — muxer (`M`) and muxed (`mN`) indicators
- Value descriptions (`VAL_`) for enum signals
- Cycle-time attributes (`BA_ "GenMsgCycleTime"`) for periodic messages

Both little-endian (Intel) and big-endian (Motorola) signals are handled correctly.
