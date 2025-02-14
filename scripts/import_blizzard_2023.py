import optparse
import pathlib
import sys
import tqdm

sys.path.append('')
from cube.networks.g2p import SimpleTokenizer
import json
import librosa
import soundfile as sf
from cube.io_utils.vocoder import MelVocoder
import numpy as np
from PIL import Image
import pysptk

tokenizer = SimpleTokenizer()


def _merge(text, phon, durs):
    hybrid = []
    phon2word = []
    frame2phon = []
    words = [w.word for w in tokenizer(text)]
    windex = 0
    cindex = 0
    dd = []
    for t, p, d in zip(text, phon, durs):
        if t.isalpha():
            hybrid.append(p)
        else:
            hybrid.append(t)
        phon2word.append(windex)
        cindex += 1
        if cindex == len(words[windex]):
            cindex = 0
            windex += 1
        dd.append(d)

    nh = []
    nd = []
    np2w = []
    delta = 0
    for t, h, d, p2w in zip(text, hybrid, durs, phon2word):
        if t.isalpha() and t != ' ' and h == '_':
            delta += 1
            continue
        nh.append(h)
        nd.append(d)
        np2w.append(p2w)

    # hard-coded frameshift of 10
    durs = nd
    cphon = 0
    total = sum(durs)
    lstart = 0
    pstart = []
    pend = []
    last = 0
    for d in durs:
        pstart.append(last)
        pend.append(last + d)
        last = pend[-1]

    for frame in range(total // 10):
        while (frame * 10) >= pend[cphon]:
            cphon += 1
            while durs[cphon] == 0:
                cphon += 1
        frame2phon.append(cphon)
    return nh, words, np2w, frame2phon


def render_spectrogram(mgc, output_file):
    bitmap = np.zeros((mgc.shape[1], mgc.shape[0], 3), dtype=np.uint8)
    mgc_min = mgc.min()
    mgc_max = mgc.max()

    for x in range(mgc.shape[0]):
        for y in range(mgc.shape[1]):
            val = (mgc[x, y] - mgc_min) / (mgc_max - mgc_min)

            color = val * 255
            bitmap[mgc.shape[1] - y - 1, x] = [color, color, color]

    img = Image.fromarray(bitmap)  # smp.toimage(bitmap)
    img.save(output_file)


def _import_audio(dataset, output_folder, input_folder, sample_rate, hop_size, prefix):
    vocoder = MelVocoder()
    wav = None
    last_file = None
    dataset.sort(key=lambda x: x['orig_filename'])
    oms = sample_rate / 1000
    for ii in tqdm.tqdm(range(len(dataset)), ncols=80):
        item = dataset[ii]
        id = "{0}_{1:08d}".format(prefix, ii)
        item['id'] = id
        if last_file != item['orig_filename']:
            wav, _ = librosa.load('{0}/{1}.wav'.format(input_folder, item['orig_filename']), sr=sample_rate)
            last_file = item['orig_filename']
        audio_segment = wav[int(item['orig_start'] * oms):int(item['orig_end'] * oms)]
        audio_segment = (audio_segment / (np.max(np.abs(audio_segment)))) * 0.98
        mel = vocoder.melspectrogram(audio_segment, sample_rate, 80, hop_size, False)
        output_base = '{0}/{1}'.format(output_folder, id)
        render_spectrogram(mel, '{0}.png'.format(output_base))
        sf.write('{0}.wav'.format(output_base), np.asarray(audio_segment * 32767, dtype=np.int16), sample_rate)
        np.save(open('{0}.mgc'.format(output_base), 'wb'), mel)
        json.dump(item, open('{0}.json'.format(output_base), 'w'))
        pitch = pysptk.rapt(audio_segment * 32767, sample_rate, hopsize=hop_size, min=60, max=400)
        np.save(open('{0}.pitch'.format(output_base), 'wb'), pitch)


def _get_real_alignments(dataset, position):
    item = dataset[position]
    # start
    new_start = item['start']
    new_end = item['end']
    import copy
    durs = copy.deepcopy(item['durs'])
    if position > 0 and dataset[position - 1]['orig_fn'] == dataset[position]['orig_fn']:  # same chapter
        prev_end = dataset[position - 1]['end'] - 130
        new_start = prev_end
        durs[0] = item['start'] - prev_end + 130

    # end
    if position < len(dataset) and dataset[position]['orig_fn'] == dataset[position + 1]['orig_fn']:
        next_start = dataset[position + 1]['start'] + 130
        new_end = next_start
        # seek what phone received the 130 duration - not always the last one
        for ii in range(len(durs) - 1, 0, -1):
            if durs[ii] == 130:
                break
        durs[ii] = next_start - item['end'] + 130

    return new_start, new_end, durs


def _correct_alignments(lines):
    dataset = []
    for ii in tqdm.tqdm(range(len(lines)), ncols=120, desc='alignments'):
        line = lines[ii].strip()
        parts = line.split('|')
        if len(parts) < 6:
            continue
        if '{' in parts[3] and '}' in parts[3]:
            continue
        durs = [int(x) for x in parts[5].strip().split(' ')]
        phon = parts[4].split(' ')
        text = parts[3]
        orig_start = int(parts[1])
        orig_end = int(parts[2])
        orig_fn = parts[0]
        dataset.append({
            'orig_fn': orig_fn,
            'start': orig_start,
            'end': orig_end,
            'text': text,
            'phon': phon,
            'durs': durs
        })

    tmp = []
    for ii in range(len(dataset)):
        new_start, new_end, new_durs = _get_real_alignments(dataset, ii)
        p0 = dataset[ii]['orig_fn']
        p1 = str(new_start)
        p2 = str(new_end)
        p3 = dataset[ii]['text']
        p4 = ' '.join(dataset[ii]['phon'])
        p5 = ' '.join([str(x) for x in new_durs])
        tmp.append('|'.join([p0, p1, p2, p3, p4, p5]))

    return tmp


def _import_dataset(params):
    lines = open(params.input_file).readlines()
    lines = _correct_alignments(lines)
    valid_sents = 0
    total_time = 0
    dataset = []
    print("Reading and processing alignment file")
    for ii in tqdm.tqdm(range(len(lines)), ncols=120):
        line = lines[ii].strip()
        parts = line.split('|')
        if len(parts) < 6:
            continue
        text = parts[3]
        if '{' in text and '}' in text:
            continue
        durs = [int(x) for x in parts[5].strip().split(' ')]
        phon = parts[4].split(' ')
        if len(text) != len(phon) or len(text) != len(durs):
            from ipdb import set_trace
            set_trace()
        hybrid, words, phon2word, frame2phone = _merge(text, phon, durs)
        total_time += sum(durs)
        valid_sents += 1
        # for ii in range(len(hybrid)):
        #     text = hybrid[ii] + " " + words[phon2word[ii]] + " -"
        #     for jj in range(len(frame2phone)):
        #         if frame2phone[jj] == ii:
        #             text += " " + str(jj)
        #     print(text)
        item = {
            'orig_start': int(parts[1]),
            'orig_end': int(parts[2]),
            'orig_filename': parts[0],
            'orig_text': text,
            'phones': hybrid,
            'words': words,
            'phon2word': phon2word,
            'frame2phon': frame2phone,
            'speaker': params.speaker
        }
        dataset.append(item)
    # creating context
    for ii in range(len(dataset)):
        l_start = max(0, ii - params.prev_sentences)
        l_end = min(len(dataset), ii + params.next_sentences + 1)
        # shrink window if we are at the beginning or end of a chapter - context not relevant here
        for jj in range(l_start, ii):
            if dataset[ii]['orig_filename'] != dataset[jj]['orig_filename']:
                l_start += 1
        for jj in range(l_end, ii, 1):
            if dataset[ii]['orig_filename'] != dataset[jj]['orig_filename']:
                l_end -= 1
        left_context = ' '.join([item['orig_text'][1:] for item in dataset[l_start:ii]])
        right_context = ' '.join([item['orig_text'][1:] for item in dataset[ii + 1:l_end]])
        dataset[ii]['left_context'] = left_context
        dataset[ii]['right_context'] = right_context

    # train-dev split
    trainset = []
    devset = []
    split = int(1.0 / params.dev_ratio)
    if split == 0:
        print("Warning: Invalid value for dev-ratio. Everything will be in the training set.")
        trainset = dataset
    elif split == 1:
        print("Warning: Invalid value for dev-ratio. Everything will be in the devset set.")
        devset = dataset
    else:
        for ii in range(len(dataset)):
            if (ii + 1) % split == 0:
                devset.append(dataset[ii])
            else:
                trainset.append(dataset[ii])

    import datetime
    print("Found {0} valid sentences, with a total audio time of {1}.".format(valid_sents, datetime.timedelta(
        seconds=(total_time / 1000))))
    print("Trainset will contain {0} examples and devset {1} examples".format(len(trainset), len(devset)))
    input_folder = params.input_file[:params.input_file.rfind('/')]
    print("Processing trainset")
    _import_audio(trainset, "data/processed/train/", input_folder, params.sample_rate, params.hop_size, params.prefix)
    print("Processing devset")
    _import_audio(devset, "data/processed/dev/", input_folder, params.sample_rate, params.hop_size, params.prefix)


if __name__ == '__main__':
    parser = optparse.OptionParser()
    parser.add_option('--input-file', action='store', dest='input_file',
                      help='File with alignments')
    parser.add_option('--prev-sentences', type='int', dest='prev_sentences', default=5,
                      help='How many previous sentences to use for context (default=5)')
    parser.add_option('--next-sentences', type='int', dest='next_sentences', default=5,
                      help='How of the following sentences to use for context (default=5)')
    parser.add_option('--dev-ratio', type='float', dest='dev_ratio', default=0.001,
                      help='Ratio between dev and train (default=0.001)')
    parser.add_option('--speaker', action='store', dest='speaker', default="none",
                      help='What label to use for the speaker (default="none")')
    parser.add_option('--sample-rate', type='int', dest='sample_rate', default=24000,
                      help='Upsample or downsample data to this sample-rate (default=24000)')
    parser.add_option('--hop-size', type='int', dest='hop_size', default=240,
                      help='Frame analysis hop-size (default=240)')
    parser.add_option('--prefix', dest='prefix', default='FILE',
                      help='What prefix to use for the filenames')

    (params, _) = parser.parse_args(sys.argv)
    if params.input_file:
        _import_dataset(params)
    else:
        parser.print_help()
