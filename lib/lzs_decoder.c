/*
 
Copyright (c) 2011, 2012, Simon Howard

Permission to use, copy, modify, and/or distribute this software
for any purpose with or fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL
WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE
AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR
THE USE OR OTHER DEALINGS IN THE SOFTWARE.
 
 */

#include <stdlib.h>
#include <string.h>
#include <inttypes.h>

#include "lha_decoder.h"

#include "bit_stream_reader.c"

// Parameters for ring buffer, used for storing history.  This acts
// as the dictionary for copy operations.
//
// The classic -lzs- method always uses an 11 bit position and a 4 bit
// length, giving a 2 KiB window.  The LArc 7 methods -lz1-, -lz2- and
// -lz3- store the number of bits that are used in the archive header,
// so that a different window size can be selected.

#define RING_BUFFER_SIZE 2048
#define START_OFFSET 17

// Threshold offset.  In the copy operation, the copy length is a 4-bit
// value, giving a range 0..15.  The threshold offsets this so that
// it is interpreted as 2..17 - a more useful range.

#define THRESHOLD 2

// Maximum size of the ring buffer.  LArc's dictionary is 8 KiB, so the
// position can use at most 13 bits.

#define MAX_RING_BUFFER_SIZE 8192
#define MAX_POS_BITS 13
#define MAX_LEN_BITS 13

// Size of output buffer.  A copy operation can be longer than this, in
// which case it is continued over multiple calls to the read function.

#define OUTPUT_BUFFER_SIZE 4096

// Decoder for the -lzs- compression method used by old versions of LArc.
//
// The input stream consists of commands, each of which is either "output
// a literal byte value" or "copy block". A bit at the start of each
// command signals which command it is.

typedef struct {
	BitStreamReader bit_stream_reader;
	uint8_t *ringbuf;
	unsigned int ringbuf_size;
	unsigned int ringbuf_mask;
	unsigned int ringbuf_pos;
	unsigned int pos_bits;
	unsigned int len_bits;
	unsigned int threshold;
	// A copy operation that did not fit into the output buffer in one
	// go is continued by later calls to the read function.
	unsigned int copy_start;
	unsigned int copy_len;
} LHALZSDecoder;

// Set up the ring buffer to match the current parameters.  Returns
// non-zero for success.

static int lha_lzs_setup(LHALZSDecoder *decoder)
{
	unsigned int size = 1u << decoder->pos_bits;
	unsigned int ce2 = (decoder->pos_bits + decoder->len_bits + 1) / 9;

	// The copy length must fit inside the window.

	if ((1u << decoder->len_bits) + ce2 > size) {
		return 0;
	}

	if (decoder->ringbuf != NULL) {
		free(decoder->ringbuf);
		decoder->ringbuf = NULL;
	}

	decoder->ringbuf = malloc(size);

	if (decoder->ringbuf == NULL) {
		return 0;
	}

	// The history buffer is filled with spaces.

	memset(decoder->ringbuf, ' ', size);

	decoder->ringbuf_size = size;
	decoder->ringbuf_mask = size - 1;
	decoder->threshold = ce2 + 1;
	decoder->ringbuf_pos = (size - ((1u << decoder->len_bits) + ce2))
	                    & decoder->ringbuf_mask;
	decoder->copy_len = 0;

	return 1;
}

static int lha_lzs_init(void *data, LHADecoderCallback callback,
                        void *callback_data)
{
	LHALZSDecoder *decoder = data;

	decoder->ringbuf = NULL;
	decoder->pos_bits = 11;
	decoder->len_bits = 4;
	decoder->copy_start = 0;
	decoder->copy_len = 0;
	bit_stream_reader_init(&decoder->bit_stream_reader, callback,
	                       callback_data);

	return lha_lzs_setup(decoder);
}

static void lha_lzs_free(void *data)
{
	LHALZSDecoder *decoder = data;

	if (decoder->ringbuf != NULL) {
		free(decoder->ringbuf);
		decoder->ringbuf = NULL;
	}
}

// Set the number of bits used for the position and the length of a copy
// operation.  This must be called before any data is read.  Returns
// non-zero for success.

int lha_lzs_decoder_set_params(void *data, unsigned int pos_bits,
                               unsigned int len_bits)
{
	LHALZSDecoder *decoder = data;

	if (pos_bits < 1 || pos_bits > MAX_POS_BITS
	 || len_bits < 1 || len_bits > MAX_LEN_BITS) {
		return 0;
	}

	decoder->pos_bits = pos_bits;
	decoder->len_bits = len_bits;

	return lha_lzs_setup(decoder);
}

// Add a single byte to the output buffer.

static void output_byte(LHALZSDecoder *decoder, uint8_t *buf,
                        size_t *buf_len, uint8_t b)
{
	buf[*buf_len] = b;
	++*buf_len;

	decoder->ringbuf[decoder->ringbuf_pos] = b;
	decoder->ringbuf_pos = (decoder->ringbuf_pos + 1)
	                      & decoder->ringbuf_mask;
}

// Output a "block" of data from the specified range in the ring buffer.
// At most OUTPUT_BUFFER_SIZE bytes are copied; if the block is longer,
// the rest of it is copied by subsequent calls.

static void output_block(LHALZSDecoder *decoder,
                         uint8_t *buf,
                         size_t *buf_len,
                         unsigned int start,
                         unsigned int len)
{
	unsigned int i;
	unsigned int count = len;

	if (count > OUTPUT_BUFFER_SIZE) {
		count = OUTPUT_BUFFER_SIZE;
	}

	for (i = 0; i < count; ++i) {
		output_byte(decoder, buf, buf_len,
		            decoder->ringbuf[(start + i)
		                             & decoder->ringbuf_mask]);
	}

	decoder->copy_start = (decoder->copy_start + count)
	                    & decoder->ringbuf_mask;
	decoder->copy_len -= count;
}

// Process a single command from the LZS input stream.

static size_t lha_lzs_read(void *data, uint8_t *buf)
{
	LHALZSDecoder *decoder = data;
	int bit;
	size_t result;

	// Start from an empty buffer.

	result = 0;

	// Continue a copy operation that did not fit into the output
	// buffer in one go.

	if (decoder->copy_len > 0) {
		output_block(decoder, buf, &result,
		             decoder->copy_start, decoder->copy_len);
		return result;
	}

	// Each command starts with a bit that signals the type:

	bit = read_bit(&decoder->bit_stream_reader);

	if (bit < 0) {
		return 0;
	}

	// What type of command is this?

	if (bit) {
		int b;

		b = read_bits(&decoder->bit_stream_reader, 8);

		if (b < 0) {
			return 0;
		}

		output_byte(decoder, buf, &result, (uint8_t) b);
	} else {
		int pos, len;

		pos = read_bits(&decoder->bit_stream_reader,
		                decoder->pos_bits);
		len = read_bits(&decoder->bit_stream_reader,
		                decoder->len_bits);

		if (pos < 0 || len < 0) {
			return 0;
		}

		// The copy length is offset by the threshold, and can be
		// longer than the output buffer, in which case it is
		// continued by later calls.

		decoder->copy_start = (unsigned int) pos;
		decoder->copy_len = (unsigned int) len + decoder->threshold;

		output_block(decoder, buf, &result,
		             decoder->copy_start, decoder->copy_len);
	}

	return result;
}

const LHADecoderType lha_lzs_decoder = {
	lha_lzs_init,
	lha_lzs_free,
	lha_lzs_read,
	sizeof(LHALZSDecoder),
	OUTPUT_BUFFER_SIZE,
	RING_BUFFER_SIZE
};
