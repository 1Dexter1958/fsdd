`timescale 1ns / 1ps
//////////////////////////////////////////////////////////////////////////////////
// Company: 
// Engineer: 
// 
// Create Date: 2022/02/18 12:56:58
// Design Name: 
// Module Name: tb_New_Top
// Project Name: 
// Target Devices: 
// Tool Versions: 
// Description: 
// 
// Dependencies: 
// 
// Revision:
// Revision 0.01 - File Created
// Additional Comments:
// 
//////////////////////////////////////////////////////////////////////////////////


module tb_New_Top(

    );
    reg CLK;
    reg RST_N;
    reg Key_Signal;
    reg uart_rx;
    reg [7:0] image[0:255];
    reg [7:0] image2[0:255];
    // reg [7:0] image[0:1023];
    // reg [7:0] image1[0:783];

    Top u_Top( 
    .In_CLK(CLK),
    .RST_N(RST_N),
    .Key_Signal(Key_Signal),
    .uart_rx(uart_rx)
    );
    wire cal_success;

    assign cal_success = u_Top.pic_cal_done_flag == 1;
    always #10 CLK = ~CLK;
    integer i,j,k;
    initial begin
        // $readmemh("E:/Python_Project/Acce_Test/lxb.txt",image);
        // $readmemh("E:/Python_Project/Acce_Test/lxb1.txt",image1);
        //$readmemh("C:/Users/run/Desktop/run/coe/32_attention/traffic.txt",image);
        $readmemh("C:/Users/86139/Desktop/SRNN_FPGA/SFRNN_FSDD/project_1/project_1.srcs/sources_1/Img_data/1.txt",image);
        $readmemh("C:/Users/86139/Desktop/SRNN_FPGA/SFRNN_FSDD/project_1/project_1.srcs/sources_1/Img_data/5.txt",image2);

        CLK = 0;
        RST_N = 0;
        Key_Signal = 1;
        #100;
        @(posedge CLK) begin
            RST_N = 1; 
        end
        #50;
        j = 0;
        //@(posedge CLK)begin
        //    for(i=0;i<1024;i=i+1)begin
        //        uart_tx_byte(image[j]);
        //        j = j + 1;
        //    end
        //end
        for(k=0;k<1;k=k+1)begin
            @(posedge CLK)begin
                for(i=0;i<256;i=i+1)begin
                    uart_tx_byte(image[j]);
                    j = j + 1;
                end
            end
        end

            #10000;
            @(posedge cal_success)begin
               j = 0; // 重置j用于image2数组的索引
               for(k=0; k<1; k=k+1) begin
               @(posedge CLK); // 等待时钟上升沿
                   for(i=0; i<256; i=i+1) begin
                       uart_tx_byte(image2[j]); // 使用image2数组
                       j = j + 1;
                   end
               end
            end
        // end
        #10000;
        @(posedge CLK) begin
            #1;
            Key_Signal = 0; 
        end
        #20;
        @(posedge CLK) begin
            #1;
            Key_Signal = 1; 
        end
    end
    task uart_tx_byte;
        input [7:0]tx_data;
        begin
            uart_rx = 1;
            #8680;
            uart_rx = 0;
            #8680;
            uart_rx = tx_data[0];
            #8680;
            uart_rx = tx_data[1];
            #8680;
            uart_rx = tx_data[2];
            #8680;
            uart_rx = tx_data[3];
            #8680;
            uart_rx = tx_data[4];
            #8680;
            uart_rx = tx_data[5];
            #8680;
            uart_rx = tx_data[6];
            #8680;
            uart_rx = tx_data[7];
            #8680;
            uart_rx = 1;
            #8680;         
        end
    endtask

endmodule

