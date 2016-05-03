#! /usr/bin/env python

# bdf.py -- PBD 2015/06

# This class provides access to EVLA/ALMA Binary Data Format (BDF)
# files.  The approach used here is based loosely on 'bdfparse.py' 
# originally by Peter Williams.  So far this is somewhat targeted
# to EVLA/WIDAR data files, it may not handle all the ALMA variants.

from lxml import etree, objectify

import os
import sys
import string
import re
import mmap
import math
import numpy
from copy import deepcopy

from .mime import MIMEPart, MIMEHeader

def basename_noext(path):
    return os.path.basename(os.path.splitext(path)[0])

# TODO find a better way to get the namespace automatically?
_ns = '{http://Alma/XASDM/sdmbin}'
def _stripns(tag):
    return re.sub('{.+}','',tag)

def ant2bl(i,j=None):
    """Returns baseline index for given antenna pair.  Will accept
    two args, or a list/tuple/etc.  Uses 0-based indexing"""
    if j is None:
        (a1,a2) = sorted(i[:2])
    else:
        (a1,a2) = sorted((i,j))
    # could raise error if a2==a1, either are negative, etc
    return (a2*(a2-1))/2 + a1

def bl2ant(i):
    """Returns antenna pair for given baseline index.  All are 0-based."""
    a2 = int(0.5*(1.0+math.sqrt(1.0+8.0*i)))
    a1 = i - a2*(a2-1)/2
    return a1, a2

class BDF(object):
    """
    Class representing a single BDF file.  For example:

        b = bdf.BDF('uid____evla_bdf_1433189755525')

    Individual integration data is returned as BDFIntegration objects via
    either b.get_integration(idx) or b[idx].  Other useful methods include:

        b.basebands      # list of baseband ids
        b.spws           # dict of spectral windows per baseband
        b.numAntenna     # number of antennas
        b.numBaseline    # number of baselines
        b.numIntegration # number of integrations in file
        b.sdmDataHeader  # lxml objectify version of full header

    """

    def __init__(self, fname):
        self.fname = fname
        self.fp = open(fname, 'r')
        self.mmdata = mmap.mmap(self.fp.fileno(), 0, mmap.MAP_PRIVATE,
                mmap.PROT_READ)
        self.read_mime()
        self.parse_spws()

    # Size in bytes of each data element. In principle, the crossData 
    # type needs to be read from the headers while all others have 
    # pre-set values...
    bin_dtype_size = {
            'flags':           4, # INT32
            'actualTimes':     8, # INT64
            'actualDurations': 8, # INT64
            'zeroLags':        4, # FLOAT32
            'autoData':        4, # FLOAT32
            'crossData':       4, # FLOAT32 but should be determined from file
            }

    # Basic data type for each array.  Note, does not necessarily
    # match up with the sizes above due to how BDF defines the 'size'
    # attribute..
    bin_dtype = {
            'autoData':        numpy.float32,
            'crossData':       numpy.complex64,
            }

    def read_mime(self,full_read=False):
        self.fp.seek(0,0) # Go back to start
        if not self.fp.readline().startswith('MIME-Version:'):
            raise RuntimeError('Invalid BDF: missing MIME-Version')

        # First we need to read and parse only the main XML header in order
        # to get sizes of the binary parts.  Note, the info stored in 
        # self.bin_size is in bytes, rather than the weird BDF units.
        mime_hdr = MIMEPart(self.fp).hdr
        self.top_mime_bound = mime_hdr.boundary
        sdmDataMime = MIMEPart(self.fp,boundary=self.top_mime_bound)
        if sdmDataMime.loc != 'sdmDataHeader.xml':
            raise RuntimeError('Invalid BDF: missing sdmDataHeader.xml')
        self.sdmDataHeader = objectify.fromstring(sdmDataMime.body)
        self.bin_size = {}
        self.bin_axes = {}
        for e in self.sdmDataHeader.iter():
            if 'size' in e.attrib.keys() and 'axes' in e.attrib.keys():
                binname = _stripns(e.tag)
                self.bin_size[binname] = int(e.attrib['size']) \
                        * self.bin_dtype_size[binname]
                self.bin_axes[binname] = e.attrib['axes'].split()

        # For EVLA, we can read the first integration, note the file offset
        # in order to determine the size, then seek to each integration as
        # requested, rather than parsing the whole file here.
        if 'EVLA' in mime_hdr['Content-Description'][0] and not full_read:
            self.offset_ints = self.fp.tell() # Offset in file to first integ
            self.mime_ints = [MIMEPart(self.fp,
                boundary=self.top_mime_bound,
                binary_size=self.bin_size,
                recurse=True),]
            # Compute size of each integration section:
            self.size_ints = self.fp.tell() - self.offset_ints
            numints = int((os.path.getsize(self.fname)-self.offset_ints)/self.size_ints)
            self.mime_ints += [None,]*(numints-1)

        # This is the more general way to do it that does not assume
        # each integration (including XML and MIME headers) has the 
        # same size in the file.  In this case, go back to the beginning 
        # and parse the whole MIME structure to map it out.
        else:
            self.fp.seek(0,0) # reset to start again
            full_mime = MIMEPart(self.fp,
                    recurse=True, binary_size=self.bin_size)
            self.mime_ints = full_mime.body[1:]

    def _raw(self,idx):
        if self.mime_ints[idx] is not None: return self.mime_ints[idx]
        # Need to read this one
        self.fp.seek(self.offset_ints + idx*self.size_ints, 0)
        #self.mime_ints[idx] = self.read_mime_part(boundary=self.top_mime_bound,recurse=True)
        self.mime_ints[idx] = MIMEPart(self.fp, 
                boundary=self.top_mime_bound, 
                binary_size=self.bin_size,
                recurse=True)
        return self.mime_ints[idx]

    @property
    def projectPath(self):
        return self.sdmDataHeader.attrib['projectPath']

    @property
    def numIntegration(self):
        return len(self.mime_ints)

    @property
    def numAntenna(self):
        return int(self.sdmDataHeader.numAntenna)

    @property
    def numBaseline(self):
        return (self.numAntenna*(self.numAntenna-1))/2

    @property
    def startTime(self):
        return float(self.sdmDataHeader.startTime)/86400.0e9

    def parse_spws(self):
        self.basebands = []
        self.spws = {}
        # Offsets into the cross and data arrays where this spw
        # can be found.  This info could be reconstructed later
        # but seems convenient to do it here.
        cross_offset = 0
        auto_offset = 0
        for bb in self.sdmDataHeader.dataStruct.baseband:
            bbname = bb.attrib['name']
            self.basebands.append(bbname)
            self.spws[bbname] = []
            for spw_elem in bb.spectralWindow:
                # Build a list of spectral windows for each baseband
                spw = SpectralWindow(spw_elem,cross_offset,auto_offset)
                cross_offset += spw.dsize('cross')
                auto_offset += spw.dsize('auto')
                self.spws[bbname].append(spw)

    def get_integration(self,idx):
        return BDFIntegration(self,idx)

    def __getitem__(self,idx):
        return self.get_integration(idx)

    def get_data(self,baseband,spw,type='cross',scrunch=False,
            fscrunch=False,frange=None):
        """Returns an array containing all integrations for the specified
        baseband, spw and data type.  If scrunch=True, all integrations
        will be averaged."""
        chidx = -2 # index of spectral channels
        # Read first integration to get shapes, etc
        subdat = self.get_integration(0).get_data(baseband,spw,type)
        if scrunch:
            dshape = subdat.shape
        else:
            dshape = (self.numIntegration,) + subdat.shape
        if fscrunch:
            dshape = dshape[:chidx] + dshape[chidx+1:]
        result = numpy.zeros(dshape,dtype=subdat.dtype)
        for i in range(self.numIntegration):
            if fscrunch:
                if frange is None:
                    dat = self.get_integration(i).get_data(baseband,spw,type).mean(chidx)
                else:
                    dat = self.get_integration(i).get_data(baseband,spw,type).take(range(*frange),axis=chidx).mean(chidx)
            else:
                dat = self.get_integration(i).get_data(baseband,spw,type)
            if scrunch:
                result += dat
            else:
                result[i] = dat
        if scrunch: 
            result /= float(self.numIntegration)
        return result

class SpectralWindow(object):
    """Spectral window class.  Initialize from the XML element."""

    def __init__(self, spw_elem, cross_offset=None, auto_offset=None):
        self._attrib = spw_elem.attrib
        self.cross_offset = cross_offset
        self.auto_offset = auto_offset

    @property
    def numBin(self):
        return int(self._attrib['numBin'])

    @property
    def numSpectralPoint(self):
        return int(self._attrib['numSpectralPoint'])

    # These two are not listed in the BDF spec but appear in EVLA data:
    @property
    def sw(self):
        return int(self._attrib['sw'])

    @property
    def swbb(self):
        return self._attrib['swbb']

    # Should uniquely identify a sw?
    @property
    def name(self):
        return self.swbb + '-' + str(self.sw)

    def npol(self,type):
        """Return number of polarization array elements for the given data
        type (cross or auto)."""
        try:
            if type[0].lower()=='c':
                return len(self._attrib['crossPolProducts'].split())
            elif type[0].lower()=='a':
                # Good enough?
                l = len(self._attrib['sdPolProducts'].split())
                return 4 if l==3 else l # 3==4 in BDF math! :)
        except KeyError:
            return 0

    def dshape(self,type):
        """Return shape tuple of data array for this spectral window, 
        in number of data elements (real for auto, complex for cross)."""
        return (self.numBin, self.numSpectralPoint, self.npol(type))

    def dsize(self,type):
        """Return size of data array for this spectral window, in number of
        data elements (real for auto, complex for cross)."""
        return numpy.product(self.dshape(type))

class BDFIntegration(object):
    """
    Describes and holds data for a single intgration within a BDF file.
    This should be derived from an existing BDF object using 
    get_integration() or indexing, ie:

        b = bdf.BDF('some_file')

        # Get the 5th integration, these two are equivalent:
        i = b.get_integration(5)
        i = b[5]

        # Read the cross-corr data array for spectral window 0 in 
        # the AC baseband:
        dat = i.get_data('AC_8BIT',0)

    Other potentially useful info:

        i.basebands            # list of baseband IDs
        i.spws                 # dict of spws per baseband
        i.numAntenna           # obvious
        i.numBaseline          # "
        i.sdmDataSubsetHeader  # lxml objectify version of full sub-header

    """

    def __init__(self,bdf,idx):
        # Get the main header
        self.sdmDataSubsetHeader = objectify.fromstring(
                bdf._raw(idx).body[0].body)
        # Copy some info from the BDF headers
        self.basebands = bdf.basebands
        self.spws = bdf.spws
        self.bin_axes = bdf.bin_axes
        self.numAntenna = bdf.numAntenna
        self.numBaseline = bdf.numBaseline
        # Get the binary data
        self.data = {}
        for m in bdf._raw(idx).body[1:]:
            btype = basename_noext(m.loc)
            bsize = bdf.bin_size[btype] # size of the binary blob in bytes
            baxes = self.bin_axes[btype]
            # Determine outer step size for the array, either baselines
            # antennas of baseline+antenna.  We can't apply the other
            # dimensions here because the number of elements can vary
            # per spw.
            if baxes[0]=='BAL' and baxes[1]=='ANT':
                shape=(self.numBaseline+self.numAntenna,-1)
            elif baxes[0]=='BAL':
                shape=(self.numBaseline,-1)
            elif baxes[0]=='ANT':
                shape=(self.numAntenna,-1)
            else:
                shape=(-1,) # Don't know what to do, just leave flat array
            self.data[m.loc] = numpy.frombuffer(bdf.mmdata[m.body:m.body+bsize],
                    dtype=bdf.bin_dtype[btype]).reshape(shape)

    @property
    def projectPath(self):
        return self.sdmDataSubsetHeader.attrib['projectPath']

    @property
    def time(self):
        return float(self.sdmDataSubsetHeader.schedulePeriodTime.time)/86400.0e9

    @property
    def interval(self):
        return float(self.sdmDataSubsetHeader.schedulePeriodTime.interval)*1e-9

    def get_data(self,baseband,spwidx,type='cross'):
        """
        Return the data array for the given subset.  Inputs are:

            baseband:  baseband ID string
            spwidx:    spw index within baseband
            type:      'cross' or 'auto' (default 'cross')

        The returned array shape is (nBl/nAnt, nBin, nSpp, nPol).
        """
        spw = self.spws[baseband][spwidx]
        if type[0].lower()=='c': 
            loc = self.projectPath + 'crossData.bin'
            offs = spw.cross_offset
        elif type[0].lower()=='a': 
            loc = self.projectPath + 'autoData.bin'
            offs = spw.auto_offset
        else:
            raise RuntimeError('Unsupported data type')
        dsize = spw.dsize(type)
        dshape = (-1,) + spw.dshape(type)
        return self.data[loc][:,offs:offs+dsize].reshape(dshape)


import numpy
from numpy import linalg
def gaincal(data,axis=0,ref=0):
    """Derives amplitude/phase calibration factors from the data array
    for the given baseline axis.  In the returned array, the baseline
    dimension is converted to antenna.  No other axes are modified.
    Note this internally makes a transposed copy of the data so be 
    careful with memory usage in the case of large data sets."""
    nbl = data.shape[axis]
    ndim = len(data.shape)
    (check,nant) = bl2ant(nbl)
    if check!=0:
        raise RuntimeError("Specified axis dimension (%d) is not a valid number of baselines" % nbl)
    tdata = numpy.zeros(data.shape[:axis]+data.shape[axis+1:]+(nant,nant),
            dtype=data.dtype)
    for i in range(nbl):
        (a0,a1) = bl2ant(i)
        tdata[...,a0,a1] = data.take(i,axis=axis)
        tdata[...,a1,a0] = numpy.conj(data.take(i,axis=axis))
    (w,v) = linalg.eigh(tdata)
    result = numpy.sqrt(w[...,-1]).T*v[...,-1].T
    # First axis is now antenna.. refer all phases to reference ant
    result = (result*numpy.conj(result[ref])/numpy.abs(result[ref])).T
    # TODO try to reduce number of transposes
    outdims = range(axis) + [-1,] + range(axis,ndim-1)
    return result.transpose(outdims)


class BDFWriter(object):
    """
    Write a BDF file.
    """
    def __init__(self, fname, bdf=None):
        """Init BDFWrite with output filename (fname).  If the bdf
        argument contains a BDF object, its header is copied for the
        output file.  Otherwise the BDFWrite.sdmDataHeader needs to 
        be populated."""
        self.fname = fname
        self.fp = None
        self.curidx = 1
        self.mb1 = "MIME_boundary-1"
        self.mb2 = "MIME_boundary-2"
        self.len0 = 0
        self.len1 = 0
        self.len2 = 0
        self.sdmDataHeader = None
        if bdf is not None: 
            self.sdmDataHeader = deepcopy(bdf.sdmDataHeader)

    def write_header(self):
        """Open output and write the current header contents."""
        self.fp = open(self.fname,'w')
        tophdr = MIMEHeader()
        tophdr['MIME-Version'] = ['1.0',]
        tophdr['Content-Type'] = ['multipart/mixed', 'boundary='+self.mb1]
        tophdr['Content-Description'] = [
                'EVLA/CORRELATOR/WIDAR/FULL_RESOLUTION',]
        # How do we generate a new unique name?
        nsxl = self.sdmDataHeader.nsmap['xl']
        uid = self.sdmDataHeader.dataOID.attrib['{%s}href'%nsxl][5:]
        tophdr['Content-Location'] = ['http://evla.nrao.edu/wcbe/XSDM' + uid,]
        self.fp.write(tophdr.tostring() + '\n')

        self.fp.write('--' + self.mb1 + '\n')
        xhdr = MIMEHeader()
        xhdr['Content-Type'] = ['text/xml', 'charset=utf-8']
        xhdr['Content-Location'] = ['sdmDataHeader.xml',]
        self.fp.write(xhdr.tostring() + '\n')
        self.fp.write(etree.tostring(self.sdmDataHeader,
            standalone=True,encoding='utf-8') + '\n')

    def write_integration(self,bdf_int):
        """Input is a BDFIntegration object.  The projectPath will be updated
        so that it is consistent for the file being written but otherwise
        no changes are made to the contents."""
        tophdr = MIMEHeader()
        tophdr['Content-Type'] = ['multipart/related', 'boundary='+self.mb2]
        tophdr['Content-Description'] = ['data and metadata subset',]

        ppidx = self.sdmDataHeader.attrib['projectPath'] + '%d/' % self.curidx

        hdr = MIMEHeader()
        hdr['Content-Type'] = ['text/xml', 'charset=utf-8']
        hdr['Content-Location'] = [ppidx + 'desc.xml']
        if self.len0==0:
            self.len0 = len(hdr.tostring()) + 12
        nxpad = self.len0 - len(hdr.tostring())
        if nxpad<0: 
            raise RuntimeError('nxpad(0)<0')
        hdr['X-pad'] = ['*'*nxpad,]

        # Update subhdr with new path
        subhdr = deepcopy(bdf_int.sdmDataSubsetHeader)
        subhdr.attrib['projectPath'] = ppidx
        nsxl = subhdr.nsmap['xl']
        dtypes = []
        mhdr = {}
        for dtype in ('crossData', 'autoData'):
            try:
                loc = ppidx + dtype + '.bin'
                getattr(subhdr,dtype).attrib['{%s}href'%nsxl] = loc
                dtypes += [dtype,]
                mhdr[dtype] = MIMEHeader()
                mhdr[dtype]['Content-Type'] = ['application/octet-stream']
                mhdr[dtype]['Content-Location'] = [loc]
            except AttributeError:
                pass

        # Figure out how much X-pad to add
        subhdr_str = etree.tostring(subhdr, standalone=True, encoding='utf-8')
        if self.len1==0:
            self.len1 = len(subhdr_str) + len(mhdr[dtypes[0]].tostring()) + 50
        nxpad = self.len1 - (len(subhdr_str) + len(mhdr[dtypes[0]].tostring()))
        mhdr[dtypes[0]]['X-pad'] = ['*'*nxpad,]

        # Assumes at most 2 data types.. TODO make more general
        if len(dtypes)>1:
            if self.len2==0:
                self.len2 = len(mhdr[dtypes[1]].tostring()) + 12
            nxpad = self.len2 - len(mhdr[dtypes[1]].tostring())
            mhdr[dtypes[1]]['X-pad'] = ['*'*nxpad,]

        # TODO could check that data sizes match up with header info..

        # Now write it all out..
        self.fp.write('--' + self.mb1 + '\n')
        self.fp.write(tophdr.tostring()+'\n')

        # XML subheader part
        self.fp.write('--' + self.mb2 + '\n')
        self.fp.write(hdr.tostring() + '\n')
        self.fp.write(subhdr_str)

        # Data parts
        for dtype in dtypes:
            self.fp.write('\n--' + self.mb2 + '\n')
            self.fp.write(mhdr[dtype].tostring() + '\n')
            orig_path = bdf_int.projectPath + dtype + '.bin'
            self.fp.write(bdf_int.data[orig_path])

        # Close out mime
        self.fp.write('\n--' + self.mb2 + '--\n')
        self.curidx += 1

    def close(self):
        self.fp.write('--' + self.mb1 + '--\n')
        self.fp.close()

