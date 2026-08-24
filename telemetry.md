5.2.1 Main OBC Telemetry (Port 0x10)
Direction: Downlink, OBC STM to host.
This asynchronous packet carries OBC state and sensor data. A current frame begins 10 79 and contains this
packed 121-byte payload:
Offset Size Type Units Meaning
0 4 u32LE count Packet sequence number
4 2 u16LE mV Main 5 V rail
6 2 u16LE mV Main 3.3 V rail
8 2 u16LE raw counts Solar LDR channel 1
10 2 u16LE raw counts Solar LDR channel 2
12 2 u16LE raw counts Solar LDR channel 3
14 2 u16LE mV Battery-charger bus voltage
16 2 i16LE mA Battery charging current
18 2 i16LE mV USB bus voltage
20 2 i16LE mA USB bus current
22 2 i16LE mV Battery output bus voltage
24 2 i16LE mA Battery output current
26 2 i16LE mV Solar input 1 bus voltage
28 2 i16LE mA Solar input 1 current
30 2 i16LE mV Solar input 2 bus voltage
32 2 i16LE mA Solar input 2 current
34 2 i16LE mV Solar input 3 bus voltage
36 2 i16LE mA Solar input 3 current
38 1 u8 flag Battery charging complete
39 1 u8 flag Charger power good
40 4 u32LE ms OBC time
44 6 3 × i16LE raw counts Magnetometer X, Y, Z
50 2 i16LE 0.1 ◦C Barometer temperature
52 4 float32 0.01 hPa Barometer pressure
56 4 float32 m Barometric altitude
60 6 3 × i16LE raw counts Accelerometer X, Y, Z
66 6 3 × i16LE raw counts Gyroscope X, Y, Z
72 4 float32 degrees GPS latitude
76 4 float32 degrees GPS longitude
80 4 float32 m GPS altitude
84 4 float32 km/h GPS speed
88 4 float32 degrees GPS course
92 4 float32 dimensionless GPS horizontal dilution of precision
96 1 u8 count GPS satellite count
97 1 u8 status GPS fix
98 12 3 × float32 degrees Orientation X, Y, Z
110 2 u16LE code OBC error; not exposed by the current UI parser
112 1 u8 version OBC firmware version
113 4 float32 dBm Receiver-appended radio RSSI
117 4 float32 dB Receiver-appended radio SNR