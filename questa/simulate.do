onbreak {quit -f}
onerror {quit -f}

vsim -t 1ps -lib xil_defaultlib FC_Weight_opt

do {wave.do}

view wave
view structure
view signals

do {FC_Weight.udo}

run -all

quit -force
