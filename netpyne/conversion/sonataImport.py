"""
conversion/sonata.py

Functions to import/export to SONATA format

Contributors: salvadordura@gmail.com
"""

import os
import sys
import tables  # requires installing hdf5 via brew and tables via pip!
from neuroml.hdf5.NeuroMLXMLParser import NeuroMLXMLParser
from neuroml.loaders import read_neuroml2_file
from pyneuroml import pynml
from . import neuromlFormat # import NetPyNEBuilder
from . import neuronPyHoc
from .. import sim, specs
import neuron
from neuron import h
h.load_file('stdgui.hoc')
h.load_file('import3d.hoc')

import pprint
pp = pprint.PrettyPrinter(depth=6)

# ------------------------------------------------------------------------------------------------------------
# Helper functions copied from https://github.com/NeuroML/NeuroMLlite/blob/master/neuromllite/SonataReader.py
# ------------------------------------------------------------------------------------------------------------

'''
    Search the strings in a config file for a substitutable value, e.g. 
    "morphologies_dir": "$COMPONENT_DIR/morphologies",
'''
def subs(path, substitutes):
    #print_v('Checking for %s in %s'%(substitutes.keys(),path))
    for s in substitutes:
        if path.startswith(s):
            path = path.replace(s,substitutes[s])
    return path

def _parse_entry(w):
    try:
        return int(w)
    except:
        try:
            return float(w)
        except:
            return w
'''
    Load a generic csv file as used in Sonata
'''
def load_csv_props(info_file):
    info = {}
    columns = {}
    for line in open(info_file):
        w = line.split()
        if len(columns)==0:
            for i in range(len(w)):
                columns[i] = _parse_entry(w[i])
        else:
            info[int(w[0])] = {}
            for i in range(len(w)):
                if i!=0:
                    info[int(w[0])][columns[i]] = _parse_entry(w[i])
    return info


def ascii_encode_dict(data):
    ascii_encode = lambda x: x.encode('ascii') if (sys.version_info[0]==2 and isinstance(x, unicode)) else x
    return dict(map(ascii_encode, pair) for pair in data.items()) 


def load_json(filename):
    import json

    with open(filename, 'r') as f:
        
        data = json.load(f, object_hook=ascii_encode_dict)
        
    return data


class EmptyCell():
    pass

# ------------------------------------------------------------------------------------------------------------
# Import SONATA 
# ------------------------------------------------------------------------------------------------------------
class SONATAImporter():

    # ------------------------------------------------------------------------------------------------------------
    # class constructor 
    # ------------------------------------------------------------------------------------------------------------
    def __init__(self, **parameters):
                     
        print("Creating SONATAImporter %s..."%parameters)
        self.parameters = parameters
        self.current_node = None
        self.current_node_group = None
        self.current_edge = None

        # check which are used
        self.cell_info = {}
        self.pop_comp_info = {}
        self.syn_comp_info = {}
        self.input_comp_info = {}
        self.edges_info = {}
        self.conn_info = {}
        self.nodes_info = {}
        
        self.pre_pop = None
        self.post_pop = None

        # added by salva
        self.pop_id_from_type = {}


    # ------------------------------------------------------------------------------------------------------------
    # Import a network by reading all the SONATA files and creating the NetPyNE structures
    # ------------------------------------------------------------------------------------------------------------
    def importNet(self, configFile):

        #from .. import sim

        # read config files
        filename = os.path.abspath(configFile)
        rootFolder = os.path.dirname(configFile)
        
        self.config = load_json(filename)
        self.substitutes = {'../': '%s/../'%rootFolder,
                            './': '%s/'%rootFolder,
                       '${configdir}': '%s'%rootFolder}

        if 'network' in self.config:
            self.network_config = load_json(subs(self.config['network'], self.substitutes))
        else:
            self.network_config = self.config
            
        if 'simulation' in self.config:
            self.simulation_config = load_json(subs(self.config['simulation'], self.substitutes))
        else:
            self.simulation_config = None
            
        for m in self.network_config['manifest']:
            path = subs(self.network_config['manifest'][m], self.substitutes)
            self.substitutes[m] = path


        # create and initialize sim object
        sim.initialize() 
        
        # create netpyne simConfig 
        self.createSimulationConfig()

        # add compiled mod folder
        modFolder = subs(self.network_config['components']['mechanisms_dir'], self.substitutes)+'/modfiles'
        neuron.load_mechanisms(str(modFolder))

        # create pops
        self.createPops()
        
        # create cells
        self.createCells()

        # # create connections
        # cells = self.createConns()

        # # create stimulation
        # cells = self.createStims()

        # Add extracted network to NetPyNE's sim object


    # ------------------------------------------------------------------------------------------------------------
    # create simulation config 
    # ------------------------------------------------------------------------------------------------------------
    def createSimulationConfig(self):
        print("\nCreating simulation configuratoon from %s"%(self.config['simulation']))
        sim.cfg.duration = self.simulation_config['run']


    # ------------------------------------------------------------------------------------------------------------
    # Set cell dynamic params into a cell rule (netParams.cellParams)
    # ------------------------------------------------------------------------------------------------------------
    def setCellRuleDynamicParamsFromNeuroml(self, cell, cellRule):
        
        segGroupKeys = set([sec.split('_')[0] for sec in cellRule['secs']])
        seg_grps_vs_nrn_sections = {segGroup: [sec for sec in cellRule['secs'] if sec.startswith(segGroup)] for segGroup in segGroupKeys}
        seg_grps_vs_nrn_sections['all'] = list(cellRule['secs'])
        inhomogeneous_parameters = {segGroup: [] for segGroup in segGroupKeys}  # how to fill in this from swc file?  

        for cm in cell.biophysical_properties.membrane_properties.channel_densities:
                      
            group = 'all' if not cm.segment_groups else cm.segment_groups
            for section_name in seg_grps_vs_nrn_sections[group]:
                gmax = pynml.convert_to_units(cm.cond_density,'S_per_cm2')
                if cm.ion_channel=='pas':
                    mech = {'g':gmax}
                else:
                    mech = {'gbar':gmax}
                erev = pynml.convert_to_units(cm.erev,'mV')
                
                cellRule['secs'][section_name]['mechs'][cm.ion_channel] = mech
                
                ion = self._determine_ion(cm)
                if ion == 'non_specific':
                    mech['e'] = erev
                else:
                    if 'ions' not in cellRule['secs'][section_name]:
                        cellRule['secs'][section_name]['ions'] = {}
                    if ion not in cellRule['secs'][section_name]['ions']:
                        cellRule['secs'][section_name]['ions'][ion] = {}
                    cellRule['secs'][section_name]['ions'][ion]['e'] = erev
        
        for cm in cell.biophysical_properties.membrane_properties.channel_density_v_shifts:
                      
            group = 'all' if not cm.segment_groups else cm.segment_groups
            for section_name in seg_grps_vs_nrn_sections[group]:
                gmax = pynml.convert_to_units(cm.cond_density,'S_per_cm2')
                if cm.ion_channel=='pas':
                    mech = {'g':gmax}
                else:
                    mech = {'gbar':gmax}
                erev = pynml.convert_to_units(cm.erev,'mV')
                
                cellRule['secs'][section_name]['mechs'][cm.ion_channel] = mech
                
                ion = self._determine_ion(cm)
                if ion == 'non_specific':
                    mech['e'] = erev
                else:
                    if 'ions' not in cellRule['secs'][section_name]:
                        cellRule['secs'][section_name]['ions'] = {}
                    if ion not in cellRule['secs'][section_name]['ions']:
                        cellRule['secs'][section_name]['ions'][ion] = {}
                    cellRule['secs'][section_name]['ions'][ion]['e'] = erev
                mech['vShift'] = pynml.convert_to_units(cm.v_shift,'mV')
                    
        for cm in cell.biophysical_properties.membrane_properties.channel_density_nernsts:
            group = 'all' if not cm.segment_groups else cm.segment_groups
            for section_name in seg_grps_vs_nrn_sections[group]:
                gmax = pynml.convert_to_units(cm.cond_density,'S_per_cm2')
                if cm.ion_channel=='pas':
                    mech = {'g':gmax}
                else:
                    mech = {'gbar':gmax}
                
                cellRule['secs'][section_name]['mechs'][cm.ion_channel] = mech
                
                #TODO: erev!!
                
                ion = self._determine_ion(cm)
                if ion == 'non_specific':
                    pass
                    ##mech['e'] = erev
                else:
                    if 'ions' not in cellRule['secs'][section_name]:
                        cellRule['secs'][section_name]['ions'] = {}
                    if ion not in cellRule['secs'][section_name]['ions']:
                        cellRule['secs'][section_name]['ions'][ion] = {}
                    ##cellRule['secs'][section_name]['ions'][ion]['e'] = erev
                    
                    
        for cm in cell.biophysical_properties.membrane_properties.channel_density_ghk2s:
                      
            group = 'all' if not cm.segment_groups else cm.segment_groups
            for section_name in seg_grps_vs_nrn_sections[group]:
                gmax = pynml.convert_to_units(cm.cond_density,'S_per_cm2')
                if cm.ion_channel=='pas':
                    mech = {'g':gmax}
                else:
                    mech = {'gbar':gmax}
                
                ##erev = pynml.convert_to_units(cm.erev,'mV')
                
                cellRule['secs'][section_name]['mechs'][cm.ion_channel] = mech
                
                ion = self._determine_ion(cm)
                if ion == 'non_specific':
                    pass
                    #mech['e'] = erev
                else:
                    if 'ions' not in cellRule['secs'][section_name]:
                        cellRule['secs'][section_name]['ions'] = {}
                    if ion not in cellRule['secs'][section_name]['ions']:
                        cellRule['secs'][section_name]['ions'][ion] = {}
                    ##cellRule['secs'][section_name]['ions'][ion]['e'] = erev
        
        for cm in cell.biophysical_properties.membrane_properties.channel_density_non_uniforms:
            
            for vp in cm.variable_parameters:
                if vp.parameter=="condDensity":
                    iv = vp.inhomogeneous_value
                    grp = vp.segment_groups
                    path_vals = inhomogeneous_parameters[grp]
                    expr = iv.value.replace('exp(','math.exp(')
                    #print("variable_parameter: %s, %s, %s"%(grp,iv, expr))
                    
                    for section_name in seg_grps_vs_nrn_sections[grp]:
                        path_start, path_end = inhomogeneous_parameters[grp][section_name]
                        p = path_start
                        gmax_start = pynml.convert_to_units('%s S_per_m2'%eval(expr),'S_per_cm2')
                        p = path_end
                        gmax_end = pynml.convert_to_units('%s S_per_m2'%eval(expr),'S_per_cm2')
                        
                        nseg = cellRule['secs'][section_name]['geom']['nseg'] if 'nseg' in cellRule['secs'][section_name]['geom'] else 1
                        
                        #print("   Cond dens %s: %s S_per_cm2 (%s um) -> %s S_per_cm2 (%s um); nseg = %s"%(section_name,gmax_start,path_start,gmax_end,path_end, nseg))
                        
                        gmax = []
                        for fract in [(2*i+1.0)/(2*nseg) for i in range(nseg)]:
                            
                            p = path_start + fract*(path_end-path_start)
                            
                            
                            gmax_i = pynml.convert_to_units('%s S_per_m2'%eval(expr),'S_per_cm2')
                            #print("     Point %s at %s = %s"%(p,fract, gmax_i))
                            gmax.append(gmax_i)
                        
                        if cm.ion_channel=='pas':
                            mech = {'g':gmax}
                        else:
                            mech = {'gbar':gmax}
                        erev = pynml.convert_to_units(cm.erev,'mV')

                        cellRule['secs'][section_name]['mechs'][cm.ion_channel] = mech

                        ion = self._determine_ion(cm)
                        if ion == 'non_specific':
                            mech['e'] = erev
                        else:
                            if 'ions' not in cellRule['secs'][section_name]:
                                cellRule['secs'][section_name]['ions'] = {}
                            if ion not in cellRule['secs'][section_name]['ions']:
                                cellRule['secs'][section_name]['ions'][ion] = {}
                            cellRule['secs'][section_name]['ions'][ion]['e'] = erev
                        
                    
        for cm in cell.biophysical_properties.membrane_properties.channel_density_ghks:
            raise Exception("<channelDensityGHK> not yet supported!")
        
        for cm in cell.biophysical_properties.membrane_properties.channel_density_non_uniform_nernsts:
            raise Exception("<channelDensityNonUniformNernst> not yet supported!")
        
        for cm in cell.biophysical_properties.membrane_properties.channel_density_non_uniform_ghks:
            raise Exception("<channelDensityNonUniformGHK> not yet supported!")
        
        
        for vi in cell.biophysical_properties.membrane_properties.init_memb_potentials:
            
            group = 'all' if not vi.segment_groups else vi.segment_groups
            for section_name in seg_grps_vs_nrn_sections[group]:
                cellRule['secs'][section_name]['vinit'] = pynml.convert_to_units(vi.value,'mV')
                    
        for sc in cell.biophysical_properties.membrane_properties.specific_capacitances:
            
            group = 'all' if not sc.segment_groups else sc.segment_groups
            for section_name in seg_grps_vs_nrn_sections[group]:
                cellRule['secs'][section_name]['geom']['cm'] = pynml.convert_to_units(sc.value,'uF_per_cm2')
                    
        for ra in cell.biophysical_properties.intracellular_properties.resistivities:
            
            group = 'all' if not ra.segment_groups else ra.segment_groups
            for section_name in seg_grps_vs_nrn_sections[group]:
                cellRule['secs'][section_name]['geom']['Ra'] = pynml.convert_to_units(ra.value,'ohm_cm')
                
        for specie in cell.biophysical_properties.intracellular_properties.species:
            
            group = 'all' if not specie.segment_groups else specie.segment_groups
            for section_name in seg_grps_vs_nrn_sections[group]:
                cellRule['secs'][section_name]['ions'][specie.ion]['o'] = pynml.convert_to_units(specie.initial_ext_concentration,'mM')
                cellRule['secs'][section_name]['ions'][specie.ion]['i'] = pynml.convert_to_units(specie.initial_concentration,'mM')
                
                cellRule['secs'][section_name]['mechs'][specie.concentration_model] = {}
                
        
        return cellRule
        

    def _determine_ion(self, channel_density):
        ion = channel_density.ion
        if not ion:
            if 'na' in channel_density.ion_channel.lower():
                ion = 'na'
            elif 'k' in channel_density.ion_channel.lower():
                ion = 'k'
            elif 'ca' in channel_density.ion_channel.lower():
                ion = 'ca'
            else:
                ion = 'non_specific'
        return ion


    # ------------------------------------------------------------------------------------------------------------
    # Create populations
    # ------------------------------------------------------------------------------------------------------------
    def createPops(self):
        
        #  Get info from nodes files    
        for n in self.network_config['networks']['nodes']:
            nodes_file = subs(n['nodes_file'], self.substitutes)
            node_types_file = subs(n['node_types_file'], self.substitutes)
            
            print("\nLoading nodes from %s and %s"%(nodes_file, node_types_file))

            h5file = tables.open_file(nodes_file,mode='r')

            self.parse_group(h5file.root.nodes)
            h5file.close()
            self.nodes_info[self.current_node] = load_csv_props(node_types_file)
            self.current_node = None

        pp.pprint(self.nodes_info)


        #  Use extracted node/cell info to create populations
        for sonata_pop in self.cell_info:
            # iterate over cell types -- will make one netpyne population per cell type
            for type in self.cell_info[sonata_pop]['type_numbers']:
                info = self.nodes_info[sonata_pop][type]
                pop_name = info['pop_name'] if 'pop_name' in info else None
                
                ref = info['model_name'] if 'model_name' in info else info['model_type']
                model_type = info['model_type']
                model_template = info['model_template'] if 'model_template' in info else '- None -'
                
                if pop_name:
                    pop_id = '%s_%s'%(sonata_pop, pop_name) 
                else:
                    pop_id = '%s_%s_%s'%(sonata_pop,ref,type) 

                self.pop_id_from_type[(sonata_pop, type)] = pop_id 
                    
                print(" - Adding population: %s which has model info: %s"%(pop_id, info))
                
                size = self.cell_info[sonata_pop]['type_numbers'][type]

                # create netpyne pop
                popTags = {}
                popTags['cellModel'] = model_type
                popTags['cellType'] = info['model_name'] if 'model_name' in info else pop_id
                popTags['numCells'] = size
                popTags['pop'] = pop_id
                popTags['ei'] = info['ei'] if 'ei' in info else ''
                sim.net.pops[pop_id] = sim.Pop(pop_id, popTags)
                sim.net.pops[pop_id].cellModelClass = sim.CompartCell

                # create population cell template (sections) from morphology and dynamics params files
                if model_type == 'biophysical':

                    # morphology
                    morphology_file = subs(self.network_config['components']['morphologies_dir'], self.substitutes) +'/'+info['morphology'] + '.swc'
                    cellMorph = EmptyCell()
                    swcData = h.Import3d_SWC_read()
                    swcData.input(morphology_file)
                    swcSecs = h.Import3d_GUI(swcData, 0)
                    swcSecs.instantiate(cellMorph)
                    secs, secLists, synMechs, globs = neuronPyHoc.getCellParams(cellMorph)
                    cellRule = {'conds': {'pop': pop_id}, 'secs': secs, 'secLists': secLists, 'globals': globs}
                    

                    # dynamics params
                    dynamics_params_file = subs(self.network_config['components']['biophysical_neuron_models_dir']+'/'+info['model_template'], self.substitutes) 
                    if info['model_template'].startswith('nml'):
                        dynamics_params_file = dynamics_params_file.replace('nml:', '')
                        nml_doc = read_neuroml2_file(dynamics_params_file)
                        cell_dynamic_params = nml_doc.cells[0]
                        cellRule = self.setCellRuleDynamicParamsFromNeuroml(cell_dynamic_params, cellRule)

                    elif info['dynamics_params'].startswith('json'):
                        dynamics_params = load_json(dynamics_params_file)
                        pass

                                    
                    # set extracted cell params in cellParams rule
                    sim.net.params.cellParams[pop_id] = cellRule

                    # clean up before next import
                    del swcSecs, cellMorph
                    h.initnrn()

                elif model_type == 'virtual':
                    pass



    # ------------------------------------------------------------------------------------------------------------
    # Create cells
    # ------------------------------------------------------------------------------------------------------------
    def createCells(self):
        for sonata_pop in self.cell_info:
            cellTypes = self.cell_info[sonata_pop]['types']
            cellLocs = self.cell_info[sonata_pop]['0']['locations']
            numCells = len(self.cell_info[sonata_pop]['types'])

            #from IPython import embed; embed()

            for icell in range(numCells):
                # set gid
                gid = sim.net.lastGid+icell
                
                # get info from pop
                cellTags = {}
                cellType = cellTypes[icell]
                pop_id = self.pop_id_from_type[(sonata_pop, cellType)]
                pop = sim.net.pops[pop_id]
                pop.cellGids.append(gid)  # add gid list of cells belonging to this population - not needed?

                model_type = pop.tags['cellModel']

                # set cell tags
                if model_type == 'biophysical':
                    cellTags = {k: v for (k, v) in pop.tags.items() if k in sim.net.params.popTagsCopiedToCells}  # copy all pop tags to cell tags, except those that are pop-specific
                    
                    cellTags['pop'] = pop.tags['pop']
                    
                    cellTags['x'] = cellLocs[icell]['x'] # set x location (um)
                    cellTags['y'] = cellLocs[icell]['y'] # set y location (um)
                    cellTags['z'] = cellLocs[icell]['z'] # set z location (um)
                    cellTags['xnorm'] = cellTags['x'] / sim.net.params.sizeX # set x location (um)
                    cellTags['ynorm'] = cellTags['y'] / sim.net.params.sizeY # set y location (um)
                    cellTags['znorm'] = cellTags['z'] / sim.net.params.sizeZ # set z location (um)
                    cellTags['rot_y'] = cellLocs[icell]['rotation_angle_yaxis']  # set y-axis rotation (implementation missing!)
                    cellTags['rot_z'] = cellLocs[icell]['rotation_angle_zaxis']  # set z-axis rotation

                    sim.net.cells.append(pop.cellModelClass(gid, cellTags)) # instantiate Cell object

                    sim.net.cells[-1].randrandRotationAngle = cellTags['rot_z']  # rotate cell in z-axis (y-axis rot missing)
                    

                elif model_type == 'virtual':
                    # if 'spkTimes' in self.tags:  # if VecStim, copy spike times to params
                    #     if isinstance(self.tags['spkTimes'][0], list):
                    #         try:
                    #             cellTags['params']['spkTimes'] = self.tags['spkTimes'][i] # 2D list
                    #         except:
                    #             pass
                    #     else:
                    #         cellTags['params']['spkTimes'] = self.tags['spkTimes'] # 1D list (same for all)

                    pass


                print(('Cell %d/%d (gid=%d) of pop %s, on node %d, '%(icell, numCells, gid, pop_id, sim.rank)))
            sim.net.lastGid = sim.net.lastGid + numCells 





    # ------------------------------------------------------------------------------------------------------------
    # Create connections
    # ------------------------------------------------------------------------------------------------------------
    def createConns(self):
        #  Get info from edges files    
        for e in self.network_config['networks']['edges']:
            edges_file = subs(e['edges_file'], self.substitutes)
            edge_types_file = subs(e['edge_types_file'], self.substitutes)
            
            print("\nLoading edges from %s and %s"%(edges_file,edge_types_file))

            h5file=tables.open_file(edges_file,mode='r')

            print("Opened HDF5 file: %s" % (h5file.filename))
            self.parse_group(h5file.root.edges)
            h5file.close()
            self.edges_info[self.current_edge] = load_csv_props(edge_types_file)
            self.current_edge = None


    # ------------------------------------------------------------------------------------------------------------
    # Create stimulation
    # ------------------------------------------------------------------------------------------------------------
    def createStims(self):
        #  Extract info from inputs in simulation_config
        
        pp.pprint(self.simulation_config)
        
        for input in self.simulation_config['inputs']:
            info = self.simulation_config['inputs'][input]
            print(" - Adding input: %s which has info: %s"%(input, info)) 
            
            self.input_comp_info[input] = {}
            
            node_set = info['node_set']
            node_info = self.cell_info[node_set]
            print(node_info)
            from pyneuroml.plot.PlotSpikes import read_sonata_spikes_hdf5_file
            
            ids_times = read_sonata_spikes_hdf5_file(info['input_file'])
            for id in ids_times:
                times = ids_times[id]
                pop_id, cell_id = node_info['pop_map'][id] 
                print("Cell %i in Sonata node set %s (cell %s in nml pop %s) has %i spikes"%(id, node_set, pop_id, cell_id, len(times)))
                
                component = '%s_timedInputs_%i'%(input,cell_id)
                
                self.input_comp_info[input][component] ={'id': cell_id, 'times': times}
                
                input_list_id = 'il_%s_%i'%(input,cell_id)
                self.handler.handle_input_list(input_list_id, 
                                               pop_id, 
                                               component, 
                                               1)
                
                self.handler.handle_single_input(input_list_id, 
                                                  0, 
                                                  cellId = cell_id, 
                                                  segId = 0, 
                                                  fract = 0.5)
            



    # ------------------------------------------------------------------------------------------------------------
    # Create configuration 
    # ------------------------------------------------------------------------------------------------------------


    def parse_group(self, g):
        print("+++++++++++++++Parsing group: "+ str(g)+", name: "+g._v_name)

        for node in g:
            #print("   ------Sub node: %s, class: %s, name: %s (parent: %s)"   % (node,node._c_classid,node._v_name, g._v_name))

            if node._c_classid == 'GROUP':
                if g._v_name=='nodes':
                    node_id = node._v_name.replace('-','_')
                    self.current_node = node_id
                    self.cell_info[self.current_node] = {}
                    self.cell_info[self.current_node]['types'] = {}
                    self.cell_info[self.current_node]['type_numbers'] = {}
                    #self.pop_locations[self.current_population] = {}
                    
                if g._v_name==self.current_node:
                    node_group = node._v_name
                    self.current_node_group = node_group
                    self.cell_info[self.current_node][self.current_node_group] = {}
                    self.cell_info[self.current_node][self.current_node_group]['locations'] = {}
                    
                if g._v_name=='edges':
                    edge_id = node._v_name.replace('-','_')
                    # print('  Found edge: %s'%edge_id)
                    self.current_edge = edge_id
                    self.conn_info[self.current_edge] = {}
                
                if g._v_name==self.current_edge:
                    
                    self.current_pre_node = g._v_name.split('_to_')[0]
                    self.current_post_node = g._v_name.split('_to_')[1]
                    # print('  Found edge %s -> %s'%(self.current_pre_node, self.current_post_node))
                    self.conn_info[self.current_edge]['pre_node'] = self.current_pre_node
                    self.conn_info[self.current_edge]['post_node'] = self.current_post_node
                    
                self.parse_group(node)

            if self._is_dataset(node):
                self.parse_dataset(node)
                
        self.current_population = None
        self.current_node_group = None
    

    def _is_dataset(self, node):
          return node._c_classid == 'ARRAY' or node._c_classid == 'CARRAY'   


    def parse_dataset(self, d):
        print("Parsing dataset/array: %s; at node: %s, node_group %s"%(str(d), self.current_node, self.current_node_group))
                
        if self.current_node_group:
            for i in range(0, d.shape[0]):
                #index = 0 if d.name=='x' else (1 if d.name=='y' else 2)
                
                if not i in self.cell_info[self.current_node][self.current_node_group]['locations']:
                    self.cell_info[self.current_node][self.current_node_group]['locations'][i] = {}
                self.cell_info[self.current_node][self.current_node_group]['locations'][i][d.name] = d[i]
                
        elif self.current_node:
            
            if d.name=='node_group_id':
                for i in range(0, d.shape[0]):
                    if not d[i]==0:
                        raise Exception("Error: only support node_group_id==0!")
            if d.name=='node_id':
                for i in range(0, d.shape[0]):
                    if not d[i]==i:
                        raise Exception("Error: only support dataset node_id when index is same as node_id (fails in %s)...!"%d)
            if d.name=='node_type_id':
                for i in range(0, d.shape[0]):
                    self.cell_info[self.current_node]['types'][i] = d[i]
                    if not d[i] in self.cell_info[self.current_node]['type_numbers']:
                        self.cell_info[self.current_node]['type_numbers'][d[i]]=0
                    self.cell_info[self.current_node]['type_numbers'][d[i]]+=1
           
        elif d.name=='source_node_id':
            self.conn_info[self.current_edge]['pre_id'] = [i for i in d]
        elif d.name=='edge_type_id':
            self.conn_info[self.current_edge]['edge_type_id'] = [int(i) for i in d]
        elif d.name=='target_node_id':
            self.conn_info[self.current_edge]['post_id'] = [i for i in d]
              
        else:
            print("Unhandled dataset: %s"%d.name)



# main code
if __name__ == '__main__':
    sonataImporter = SONATAImporter()
    sonataImporter.importNet('/u/salvadord/Documents/ISB/Models/sonata/examples/300_cells/config.json')



# USE CLASS AS PADRAIG TO AVOID REINVENTING THE WHEEL!


'''
#------------------------------------------------------------------------------
# Load cells and pops from file and create NEURON objs
#------------------------------------------------------------------------------
def loadNet (filename, data=None, instantiate=True, compactConnFormat=False):
    from .. import sim

    if not data: data = _loadFile(filename)
    if 'net' in data and 'cells' in data['net'] and 'pops' in data['net']:
        if sim.rank == 0:
            sim.timing('start', 'loadNetTime')
            print('Loading net...')
            if compactConnFormat: 
                compactToLongConnFormat(data['net']['cells'], compactConnFormat) # convert loaded data to long format 
            sim.net.allPops = data['net']['pops']
            sim.net.allCells = data['net']['cells']
        if instantiate:
            # calculate cells to instantiate in this node
            if isinstance(instantiate, list):
                cellsNode = [data['net']['cells'][i] for i in range(int(sim.rank), len(data['net']['cells']), sim.nhosts) if i in instantiate]
            else:
                cellsNode = [data['net']['cells'][i] for i in range(int(sim.rank), len(data['net']['cells']), sim.nhosts)]
            if sim.cfg.createPyStruct:
                for popLoadLabel, popLoad in data['net']['pops'].items():
                    pop = sim.Pop(popLoadLabel, popLoad['tags'])
                    pop.cellGids = popLoad['cellGids']
                    sim.net.pops[popLoadLabel] = pop
                for cellLoad in cellsNode:
                    # create new CompartCell object and add attributes, but don't create sections or associate gid yet
                    # TO DO: assumes CompartCell -- add condition to load PointCell
                    cell = sim.CompartCell(gid=cellLoad['gid'], tags=cellLoad['tags'], create=False, associateGid=False)
                    try:
                        if sim.cfg.saveCellSecs:
                            cell.secs = Dict(cellLoad['secs'])
                        else:
                            createNEURONObjorig = sim.cfg.createNEURONObj
                            sim.cfg.createNEURONObj = False  # avoid creating NEURON Objs now; just needpy struct
                            cell.create()
                            sim.cfg.createNEURONObj = createNEURONObjorig
                    except:
                        if sim.cfg.verbose: print(' Unable to load cell secs')

                    try:
                        cell.conns = [Dict(conn) for conn in cellLoad['conns']]
                    except:
                        if sim.cfg.verbose: print(' Unable to load cell conns')

                    try:
                        cell.stims = [Dict(stim) for stim in cellLoad['stims']]
                    except:
                        if sim.cfg.verbose: print(' Unable to load cell stims')

                    sim.net.cells.append(cell)
                print(('  Created %d cells' % (len(sim.net.cells))))
                print(('  Created %d connections' % (sum([len(c.conns) for c in sim.net.cells]))))
                print(('  Created %d stims' % (sum([len(c.stims) for c in sim.net.cells]))))

                # only create NEURON objs, if there is Python struc (fix so minimal Python struct is created)
                if sim.cfg.createNEURONObj:
                    if sim.cfg.verbose: print("  Adding NEURON objects...")
                    # create NEURON sections, mechs, syns, etc; and associate gid
                    for cell in sim.net.cells:
                        prop = {'secs': cell.secs}
                        cell.createNEURONObj(prop)  # use same syntax as when creating based on high-level specs
                        cell.associateGid()  # can only associate once the hSection obj has been created
                    # create all NEURON Netcons, NetStims, etc
                    sim.pc.barrier()
                    for cell in sim.net.cells:
                        try:
                            cell.addStimsNEURONObj()  # add stims first so can then create conns between netstims
                            cell.addConnsNEURONObj()
                        except:
                            if sim.cfg.verbose: ' Unable to load instantiate cell conns or stims'

                    print(('  Added NEURON objects to %d cells' % (len(sim.net.cells))))

            if sim.rank == 0 and sim.cfg.timing:
                sim.timing('stop', 'loadNetTime')
                print(('  Done; re-instantiate net time = %0.2f s' % sim.timingData['loadNetTime']))
    else:
        print(('  netCells and/or netPops not found in file %s'%(filename)))
'''