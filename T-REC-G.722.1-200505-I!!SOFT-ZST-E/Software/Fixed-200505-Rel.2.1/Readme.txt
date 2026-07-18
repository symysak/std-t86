             ==============================================
                 ITU-T Recommendation G.722.1 (05/2005)
                     C-Source Code and Test Vectors
                              Release 2.1
             ==============================================

This software package contains the 16/32-bit fixed-point C source code
for the encoder and decoder of ITU-T Recommendation G.722.1 including
its Annex C.

The software implements both the 7 kHz mode of G.722.1 and the 14 kHz
mode of Annex C/G.722.1.

This version uses the basic operators provided by the ITU-T Software
Tool Library (STL2000) in ITU-T Recommendation G.191 (Ver. 1.0).

This software has been repackaged for republication with G.722.1
(2005) Cor.1 (2008-05), which corrected defects in the floating point
specification of G.722.1. For consistency, the release number has been
changed from 2.0 to 2.1, but the actual C code has not been changed,
and is identical to that of G.722.1 (2005).


COPYRIGHT AND INTELLECTUAL PROPERTY
===================================
 (c) 2005 Polycom, Inc.
 All rights reserved.


ITU CONTACTS
============
Online availablity:
http://www.itu.int/rec/T-REC-G.722.1

For distribution of updated software, please contact:
Sales Department
ITU
Place des Nations
CH-1211 Geneve 20
SWITZERLAND
email: sales@itu.int

For reporting problems, please contact the SG 16 secretariat at:
ITU-T SG 16 Secretariat
ITU
Place des Nations
CH-1211 Geneve 20
SWITZERLAND
fax: +41 22 730 5853
email: tsbsg16@itu.int


Software Notes for ITU-T Recommendation G.722.1 Annex B Test Vectors
====================================================================

To run the encoder:

encode  bit-stream-type in-file-name out-file-name bit-rate bandwidth


To run the decoder:

decode  bit-stream-type in-file-name out-file-name bit-rate bandwidth


The variables for the command line are described in the following 
table.


Variables               Comments
---------               --------

bit-stream-type         This variable must be set to "1" or "0". If 
                        it is "1", then the encoder will output a bit
                        stream in the ITU-T G.192 format. If it is 
                        "0" it will output a compacted bit stream as
                        would be typically used in a communications
                        channels.

in-file-name            If running the encoder, then this variable 
                        represents the name of the audio input file. 
                        The file is assumed to contain samples in a 
                        16bit 2's compliment format. If running the 
                        decoder, then this variable represents the 
                        name of the bit stream file.

out-file-name           If running the encoder, then this variable 
                        represents the name of the bit stream output 
                        file. If running the decoder this file 
                        represents the audio output samples in a 
                        16bit 2's compliment format.

bit-rate                This is the channel bit rate of the coder in 
                        bits per second. In case of 7kHz audio, use 
                        a value of either "24000" or "32000" for 
                        24kbit/s and 32kbit/s, respectively. In case 
                        of 14kHz audio, use "24000", "32000", or 
                        "48000" for 24kbit/s, 32kbit/s, and 48kbit/s,
                        respectively.

bandwidth               This is the audio bandwidth of the coder.
                        Use a value of either "7000" or "14000"
                        for 7kHz audio (G.722.1) and 14kHz audio 
                        (G.722.1 Extension), respectively.

Last update: 2008-06-26
[END]

